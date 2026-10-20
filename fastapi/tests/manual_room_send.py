"""真机端到端：驱动设备发送消息（203），验证"房间名 → 进入房间 → 发送"整条链路。

为什么需要这个脚本：无自定义群名的外部群，企微**头部**标题是「群聊(N)」，而**消息列表**里是
"成员A、成员B、成员C"（真机 dump 为证）。设备端只能靠列表标题 fast-in 才能重新进入房间 ——
所以服务端上报的房间名必须是列表标题，且设备端进入判定要能容忍占位标题。

本脚本通过**服务端 HTTP 接口**驱动真机（不需要 adb、不需要人工点屏幕）：下发 203，
再从「功能测试」页读回设备回执，断言 success_list。

用法：
    # 默认测三类房间：有群名的群 / 无群名外部群 / 私聊，并先回首页（确保"已退出房间"）
    ../venv/bin/python tests/manual_room_send.py --go-home
    ../venv/bin/python tests/manual_room_send.py --robot 570607 --group "123" \
        --group "张三、示例科技张三、离渊" --private 离渊

注意：会真的往企微发消息（文案带「自动测试」前缀），请在测试群/测试账号上跑。
"""

import argparse
import asyncio
import json
import re
import sys
import time

import httpx

BASE = "http://127.0.0.1:8765"
RUN_ID = time.strftime("%H%M%S")
MARK = f"（自动测试·房间定位校验 {RUN_ID}）"


async def _online(cli: httpx.AsyncClient, robot: str) -> bool:
    st = (await cli.get(f"{BASE}/api/status")).json()["data"]
    return robot in (st.get("clients") or [])


async def _cmd(cli: httpx.AsyncClient, robot: str, cmd_type: int, **extra) -> None:
    data = {"client_id": robot, "cmd_type": str(cmd_type)}
    data.update({k: str(v) for k, v in extra.items()})
    await cli.post(f"{BASE}/test/cmd_extra", data=data)


async def _latest_results(cli: httpx.AsyncClient) -> list[dict]:
    """从「功能测试」页取回设备回执（页面把最后一条数据渲染为 <pre> 里的 JSON）。"""
    html = (await cli.get(f"{BASE}/test")).text
    out = []
    for blob in re.findall(r"<pre[^>]*>(.*?)</pre>", html, re.S):
        txt = (
            blob.replace("&quot;", '"').replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&#x27;", "'")
        )
        try:
            out.append(json.loads(txt))
        except Exception:
            continue
    return out


async def send_and_wait(
    cli: httpx.AsyncClient, robot: str, title: str, text: str, room_type: int, timeout: float = 60.0
) -> dict:
    """下发 203，等设备回执（回执里 raw_msg 会带回原始指令，用它匹配本次发送）。"""
    await _cmd(cli, robot, 203, titleList=title, receivedContent=text,
               textType=1, roomType=room_type)
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(3)
        for item in await _latest_results(cli):
            raw = str(item.get("raw_msg") or "")
            if item.get("status") and text in raw:
                return item
    return {"status": "timeout", "reason": f"{timeout:.0f}s 内没等到回执"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--robot", default="570607")
    ap.add_argument("--group", action="append", default=None, help="外部群（可多次）")
    ap.add_argument("--private", default=None, help="私聊对象")
    ap.add_argument("--type-mismatch", default=None,
                    help="类型护栏用例：往这个房间发但声称 roomType=1，**期望被拒绝**（同名/不同类不许发出去）")
    ap.add_argument("--go-home", action="store_true", help="先下发 201 回首页（确保已退出房间）")
    args = ap.parse_args()

    # 只在"什么都没指定"时才用默认群，否则 --private 也会顺带跑默认群（真踩过，白等 2 分钟）
    groups = args.group or ([] if (args.private or args.type_mismatch) else ["张三、示例科技张三、离渊"])
    async with httpx.AsyncClient(timeout=30) as cli:
        if not await _online(cli, args.robot):
            print(f"❌ 设备 {args.robot} 不在线（active_connections 里没有它），先连上设备再来")
            return 2

        if args.go_home:
            print("① 下发 201 停止任务回首页（保证设备不在房间里 —— 正是以前发不出去的那个前提）")
            await _cmd(cli, args.robot, 201)
            await asyncio.sleep(6)

        cases = [(g, 1, "外部群", True) for g in groups]
        if args.private:
            cases.append((args.private, 2, "私聊", True))
        if args.type_mismatch:
            # 声称是外部群但实际是别的类型 → 必须拒绝发送（真机验证过：不校验就会发进私聊）
            cases.append((args.type_mismatch, 1, "类型不符(应拒绝)", False))

        results = []
        for i, (title, rtype, kind, expect_ok) in enumerate(cases, start=1):
            text = f"{MARK} #{i} {kind} 定位校验，请忽略"
            print(f"\n② [{i}/{len(cases)}] {kind} → {title!r} (roomType={rtype})")
            r = await send_and_wait(cli, args.robot, title, text, rtype)
            sent = r.get("status") == "success" and title in (r.get("success_list") or [])
            ok = sent if expect_ok else not sent
            results.append((kind, title, ok, r))
            print(f"   {'✅ 成功' if ok else '❌ 失败'} → {json.dumps(r, ensure_ascii=False)[:300]}")

        print("\n" + "=" * 64)
        for kind, title, ok, r in results:
            print(f"  {'✅' if ok else '❌'} {kind:4} {title!r:40} "
                  f"success={r.get('success_list')} fail={r.get('fail_list')} {r.get('reason') or ''}")
        bad = [x for x in results if not x[2]]
        print(f"\n{len(results) - len(bad)}/{len(results)} 通过" + ("" if not bad else " —— 有失败，看上面回执"))
        return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
