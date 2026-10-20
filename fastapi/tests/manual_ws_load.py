"""WebSocket 承载压测：N 台假设备连服务端（手工脚本，pytest 不收集）

为什么需要：在线设备只存在于**内存注册表** `active_connections` 里，服务端是单进程单事件循环。
"200 台设备同时在线扛不扛得住"是 `docs/云端部署方案.md` C5 之前唯一没有证据的结论，本脚本就是那份证据。

为什么它安全（不会误伤生产数据）：
- 每台假设备只发 `{"type": 11}` 心跳（`app/models/message.py:11 HEART_BEAT`，
  `aivbot_service.handle_message` 收一条回一条）。不发 101 消息 → **不会触发自动回复、任务触发、
  LLM 调用、工作流**，也不查任何业务表。
- 假设备先在 `robots` 表按 `lt0001` 这类 id 预注册，否则 `robot_service.validate_robot`
  会给未知 id **随机生成 6 位设备号**，留下一批无法辨认的垃圾行；跑完（含 Ctrl-C）按自己的 id 精确删除。

用法：
    cd fastapi && ../venv/bin/python tests/manual_ws_load.py                  # 200 台 × 10 分钟
    ../venv/bin/python tests/manual_ws_load.py --clients 30 --minutes 1       # 冒烟
    ../venv/bin/python tests/manual_ws_load.py --url ws://192.168.31.125:8765
    ../venv/bin/python tests/manual_ws_load.py --no-db --keep                 # 完全不碰数据库
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiomysql  # noqa: E402
import httpx  # noqa: E402
import websockets  # noqa: E402

from app.config import settings  # noqa: E402

HEART_BEAT = 11


def ms(seconds: float) -> str:
    return f"{seconds * 1000:.1f}"


def pct(vals, q: float) -> float:
    vals = sorted(vals)
    if not vals:
        return 0.0
    return vals[min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))]


def pid_and_rss(pattern: str):
    """服务端进程的 (pid, RSS MB)，找不到返回 (None, None)

    只认 cmdline 形如 `…/python run.py` 的进程：`pgrep -f` 会连带匹配到本脚本所在的那条
    shell 命令行（命令里就含 "python run.py" 这几个字），先按 cmdline 首尾卡死再取 RSS。
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5
        ).stdout.split()
    except Exception:
        return None, None
    for tok in out:
        try:
            pid = int(tok)
            cmd = [c for c in open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "ignore").split("\0") if c]
        except Exception:
            continue
        if len(cmd) < 2 or "manual_ws_load.py" in " ".join(cmd):
            continue
        if not (os.path.basename(cmd[0]).startswith("python") and cmd[-1].endswith("run.py")):
            continue
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS"):
                        return pid, int(line.split()[1]) / 1024
        except Exception:
            pass
        return pid, None
    return None, None


async def connect_one(cid: str, ws_url: str, res: dict):
    t0 = time.perf_counter()
    try:
        ws = await websockets.connect(
            ws_url, open_timeout=15, ping_interval=None, max_size=None
        )
        await asyncio.wait_for(ws.recv(), timeout=15)  # welcome
        res["connect_ms"].append(time.perf_counter() - t0)
        return cid, ws
    except Exception as e:
        key = f"{type(e).__name__}: {str(e)[:60]}"
        res["errors"][key] = res["errors"].get(key, 0) + 1
        return cid, None


async def hold(cid: str, ws, interval: float, deadline: float, res: dict):
    try:
        while time.perf_counter() < deadline:
            await ws.send(json.dumps({"type": HEART_BEAT}))
            t0 = time.perf_counter()
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if msg.get("type") == HEART_BEAT:
                    res["rtt_ms"].append(time.perf_counter() - t0)
                    break
            res["beats"] += 1
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        res["drops"] += 1
        key = f"{type(e).__name__}: {str(e)[:60]}"
        res["errors"][key] = res["errors"].get(key, 0) + 1
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def main() -> int:
    ap = argparse.ArgumentParser(description="WebSocket 承载压测（假设备）")
    ap.add_argument("--clients", type=int, default=200)
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--interval", type=float, default=30, help="心跳间隔秒（默认 30）")
    ap.add_argument("--url", default="ws://127.0.0.1:8765")
    ap.add_argument("--status-url", default="", help="默认由 --url 推导 /api/status")
    ap.add_argument("--prefix", default="lt", help="假设备 id 前缀（robots.robot_id 是 varchar(10)）")
    ap.add_argument("--pid-pattern", default="python run.py", help="服务端进程匹配串（采样内存）")
    ap.add_argument("--no-db", action="store_true", help="不预注册/不清理（会留下自动注册的垃圾行）")
    ap.add_argument("--keep", action="store_true", help="跑完保留假设备行")
    args = ap.parse_args()

    if len(args.prefix) + 4 > 10:
        print(f"❌ 前缀 {args.prefix} 太长，robots.robot_id 是 varchar(10)")
        return 1
    ids = [f"{args.prefix}{i:04d}" for i in range(1, args.clients + 1)]
    ws_url = args.url.rstrip("/") + "/webserver/wework/"
    status_url = args.status_url or (
        args.url.replace("ws://", "http://", 1).replace("wss://", "https://", 1).rstrip("/")
        + "/api/status"
    )

    async def status():
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                d = (await c.get(status_url)).json()
            data = d.get("data") or {}
            clients = data.get("clients") or []
            return (
                data.get("active_connections"),
                len(clients),
                (data.get("robots") or {}).get("online"),
                clients,
            )
        except Exception as e:
            return f"查询失败 {type(e).__name__}", None, None, []

    pool = None
    res = {"connect_ms": [], "rtt_ms": [], "beats": 0, "drops": 0, "errors": {}}
    try:
        if not args.no_db:
            pool = await aiomysql.create_pool(
                host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
                password=settings.MYSQL_PASSWORD, db=settings.MYSQL_DATABASE,
                charset="utf8mb4", autocommit=True,
            )
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    for cid in ids:
                        await cur.execute(
                            "INSERT INTO robots (robot_id, token, device_name, status, auto_reply) "
                            "VALUES (%s, %s, %s, 'offline', 1) "
                            "ON DUPLICATE KEY UPDATE device_name=%s",
                            (cid, "loadtest", f"LOADTEST {cid}", f"LOADTEST {cid}"),
                        )
            print(f"已预注册 {len(ids)} 台假设备（{ids[0]} … {ids[-1]}）")

        # 压测前先看有谁已经在线：生产服上真机一直在，不能拿"连接数"当注册表总数
        _, _, _, base_clients = await status()
        pre = set(base_clients or [])
        if pre:
            print(f"注意：压测前已有 {len(pre)} 台在线（{', '.join(sorted(pre))}），判定按 {len(pre)}+{len(ids)} 计算")

        pid, rss0 = pid_and_rss(args.pid_pattern)
        print(f"服务端进程：pid={pid}，基线内存 {rss0:.0f} MB" if rss0 else f"服务端进程：pid={pid}（未取到内存）")
        print(f"开始：{args.clients} 台 → {ws_url}<id>，持续 {args.minutes} 分钟，心跳每 {args.interval}s")

        conns = await asyncio.gather(*[connect_one(cid, ws_url + cid, res) for cid in ids])
        live = {cid: ws for cid, ws in conns if ws}
        print(f"连接成功 {len(live)}/{args.clients} 台")
        if res["connect_ms"]:
            print(f"连接耗时：min {ms(min(res['connect_ms']))} / p50 {ms(pct(res['connect_ms'], .5))} / "
                  f"p95 {ms(pct(res['connect_ms'], .95))} / max {ms(max(res['connect_ms']))} ms")
        for err, n in res["errors"].items():
            print(f"  连接失败 {n} 次：{err}")
        if not live:
            return 1

        await asyncio.sleep(1)
        act, n, online, _ = await status()
        exp_mid = len(pre) + len(live)
        print(f"服务端 /api/status：active_connections={act}（期望 {exp_mid}），clients={n}，online={online}")
        pid, rss1 = pid_and_rss(args.pid_pattern)
        if rss0 and rss1:
            print(f"内存：基线 {rss0:.0f} MB → 全连上 {rss1:.0f} MB "
                  f"（+{rss1 - rss0:.0f} MB，约 {(rss1 - rss0) * 1024 / len(live):.0f} KB/台）")

        deadline = time.perf_counter() + args.minutes * 60
        tasks = [
            asyncio.create_task(hold(cid, ws, args.interval, deadline, res))
            for cid, ws in live.items()
        ]
        next_report = time.perf_counter() + 30
        while time.perf_counter() < deadline:
            await asyncio.sleep(min(5, max(0.5, deadline - time.perf_counter())))
            if time.perf_counter() >= next_report:
                next_report += 30
                print(f"  … 心跳 {res['beats']} 次，掉线 {res['drops']} 台，"
                      f"RTT p50 {ms(pct(res['rtt_ms'], .5))} ms，剩余 {int(deadline - time.perf_counter())}s")

        act2, n2, _, _ = await status()
        pid, rss2 = pid_and_rss(args.pid_pattern)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(2)
        act3, n3, _, after = await status()

        print("\n── 结果 ──")
        print(f"连接：{len(live)}/{args.clients} 台成功，掉线 {res['drops']} 台")
        if res["rtt_ms"]:
            print(f"心跳 RTT：{len(res['rtt_ms'])} 次，min {ms(min(res['rtt_ms']))} / "
                  f"p50 {ms(pct(res['rtt_ms'], .5))} / p95 {ms(pct(res['rtt_ms'], .95))} / "
                  f"max {ms(max(res['rtt_ms']))} ms")
        print(f"在线数：压测中 {act} → 结束前 {act2} → 全部断开后 {act3}"
              f"（期望 {exp_mid} → {exp_mid} → {len(pre)}，压测前已有 {len(pre)} 台）")
        if rss0 and rss2:
            print(f"内存：基线 {rss0:.0f} MB → 全连上 {rss1:.0f} MB → 结束 {rss2:.0f} MB")
        left = set(after or []) - pre
        ok = act == exp_mid and act3 == len(pre) and res["drops"] == 0 and not left
        print(f"结论：{'✅' if ok else '⚠️'} 注册表计数与连接数"
              f"{'一致' if act == exp_mid else f'不一致（{act} vs 期望 {exp_mid}）'}，"
              f"断开后{'已回到原有连接数' if act3 == len(pre) else f'残留 {act3}（期望 {len(pre)}）'}"
              f"{'' if not left else f'，多出 {sorted(left)}'}")
        return 0 if ok else 1
    finally:
        if not args.no_db and not args.keep and pool:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM robots WHERE robot_id IN (%s)" % ",".join(["%s"] * len(ids)),
                        ids,
                    )
            print(f"已删除 {len(ids)} 台假设备")
        if pool:
            pool.close()
            await pool.wait_closed()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
