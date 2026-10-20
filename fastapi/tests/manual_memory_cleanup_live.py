"""长期记忆 · 清理（LLM 整理）实机 harness（手工脚本，pytest 不收集；**真实调用 LLM，消耗 API 配额**）

为什么单独一个：清理是唯一会**删记忆**的功能，而且它整段依赖一个真实模型返回**可解析的 JSON 数组**
（`memory_cleanup_service.SYSTEM_PROMPT`）—— 提示词/解析/边界任何一环没接上，都可能删错东西或白跑。

本脚本验证：
  · 只处理"自上次清理以来有新的**智能体**事实"的客户（管理员导入的人工事实不触发）；
  · 结果真被整理（重复/过时事实合并），且**人工/导入的事实一个不动**；
  · 模型返回无法解析 / 空数组时不删任何东西（用 `--simulate-bad` 跑一次，不打真实模型）；
  · 清理日志（before/after）落库，`last_cleanup_at` 推进后不再重复到期。

用法：
    cd fastapi && ../venv/bin/python tests/manual_memory_cleanup_live.py [--agent 1] [--keep] [--simulate-bad] [--scheduler]
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiomysql  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import agent_service, memory_service  # noqa: E402
from app.services.memory_cleanup_service import memory_cleanup_service  # noqa: E402

PREFIX = "ZZ清理"
SET_NAME = "【自动测试】清理用记忆集"
PASSED, FAILED = [], []


def _arg(flag, default):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def check(name, ok, extra=""):
    (PASSED if ok else FAILED).append(name)
    print(f"  {'✅' if ok else '❌'} {name}{(' — ' + str(extra)) if extra and not ok else ''}")


async def main():
    agent_id = int(_arg("--agent", 1))
    keep = "--keep" in sys.argv
    bad = "--simulate-bad" in sys.argv
    scheduler = "--scheduler" in sys.argv    # 走调度器自动触发（验证启动接线），不手工调 cleanup_set

    pool = await aiomysql.create_pool(
        host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD, db=settings.MYSQL_DATABASE,
        charset="utf8mb4", autocommit=True,
    )
    for svc in (agent_service, memory_service):
        svc.set_pool(pool)
        await svc.ensure_table()

    agent = await agent_service.get_agent(agent_id)
    assert agent, f"智能体 #{agent_id} 不存在"
    print(f"用智能体 #{agent_id} {agent.name}（{agent.provider}/{agent.model}）做清理；"
          f"模式={'假模型（不消耗配额）' if bad else '真实模型'}")

    set_id = await memory_service.create_set(SET_NAME, "自动测试用，可删")
    hot = await memory_service.add_customer(set_id, f"{PREFIX}甲", f"{PREFIX}甲-采购")
    cold = await memory_service.add_customer(set_id, f"{PREFIX}乙", "")
    # 甲：三条被取代的账期事实（应被合并）+ 一条人工事实（不许动）
    await memory_service.add_item(set_id, hot, "账期改成 40 天", source="agent")
    await memory_service.add_item(set_id, hot, "账期改成 45 天", source="agent")
    await memory_service.add_item(set_id, hot, "账期 35 天", source="agent")
    await memory_service.add_item(set_id, hot, f"人工：{PREFIX}甲只接受账期 60 天", source="manual")
    # 乙：只有人工事实 → 没有"新的智能体事实"，不该被处理
    await memory_service.add_item(set_id, cold, f"人工：{PREFIX}乙是经销商", source="manual")
    # 通用桶：也放一条智能体事实
    await memory_service.add_item(set_id, 0, "公司统一话术：报价一律含税", source="agent")
    await memory_service.set_cleanup_config(set_id, 24, agent_id)
    print(f"✅ 测试数据就绪：记忆集 #{set_id}（甲 3 条智能体+1 条人工；乙 1 条人工；通用 1 条智能体）")

    orig_chat = agent_service._chat_completion
    if bad:
        async def fake_chat(*a, **kw):
            return "抱歉，我不会整理。"
        agent_service._chat_completion = fake_chat

    try:
        before_items = await memory_service.list_items(set_id)
        due_before = [d["id"] for d in await memory_service.due_cleanup_sets()]
        check("开启清理且到期 → 出现在待清理列表", set_id in due_before, due_before)

        if scheduler:
            # 不手工调用：起调度器，等它的首轮（启动后 30s 检查一次）自己发现并清理
            print("  等待调度器自动触发（最多 ~90s）…")
            await memory_cleanup_service.start()
            logs = []
            # 等"整轮跑完"的标志：last_cleanup_at 推进后该集合不再出现在待清理列表里。
            # （只看"有没有日志行"会撞上跑了一半的中间态：日志是按客户逐条写的）
            for _ in range(45):
                await asyncio.sleep(2)
                logs = await memory_service.list_cleanup_logs(set_id, limit=10)
                if logs and set_id not in [d["id"] for d in await memory_service.due_cleanup_sets()]:
                    break
            await asyncio.sleep(1)   # 让最后一条日志落库
            logs = await memory_service.list_cleanup_logs(set_id, limit=10)
            await memory_cleanup_service.stop()
            check("调度器自动发现到期的记忆集并清理", bool(logs), logs)
            summary = {
                "ok": bool(logs), "customers": len(logs),
                "before": sum(l["before_count"] for l in logs),
                "after": sum(l["after_count"] for l in logs),
                "skipped": sum(1 for l in logs if l["status"] == "skipped"),
                "failed": sum(1 for l in logs if l["status"] == "failed"),
                "note": "scheduler",
            }
        else:
            summary = await memory_cleanup_service.cleanup_set(set_id, trigger="live-test")
        print(f"  清理汇总: {summary}")

        items = await memory_service.list_items(set_id)
        agent_items = [i for i in items if i["source"] == "agent"]
        manual_items = [i for i in items if i["source"] == "manual"]
        logs = await memory_service.list_cleanup_logs(set_id, limit=10)
        print("  清理后条目：")
        for i in items:
            print(f"    [{i['source']}] {i['content'][:60]}")
        print("  清理日志：")
        for l in logs:
            print(f"    {l['customer_name'] or '通用'} | {l['status']} | "
                  f"{l['before_count']} → {l['after_count']} | {l['note']}")

        # 人工事实永远不许变
        check("人工/导入的事实一条不动",
              sorted(i["content"] for i in manual_items) ==
              sorted(i["content"] for i in before_items if i["source"] == "manual"),
              [i["content"] for i in manual_items])
        if bad:
            check("模型输出无法解析 → 什么都不删（两个客户都 failed + 0 变化）",
                  summary["failed"] == 2 and summary["before"] == 0 and summary["after"] == 0, summary)
            check("智能体事实原样保留", len(agent_items) == len(before_items) - len(manual_items),
                  len(agent_items))
        else:
            check("清理真的跑了（有 ok 日志）",
                  any(l["status"] == "ok" for l in logs), logs)
            check("过时事实被合并（智能体条目变少）",
                  len([i for i in agent_items if i["customer_id"] == hot]) < 3,
                  [i["content"] for i in agent_items])
            check("没有新增的客户被跳过",
                  all(l["customer_name"] != f"{PREFIX}乙" for l in logs),
                  [(l["customer_name"], l["status"]) for l in logs])
            ok_log = next((l for l in logs if l["status"] == "ok"), None)
            check("日志留了 before/after 变化", bool(ok_log and ok_log["before_count"] > 0), ok_log)
        check("清理后 last_cleanup_at 推进 → 不再到期",
              set_id not in [d["id"] for d in await memory_service.due_cleanup_sets()])
    finally:
        agent_service._chat_completion = orig_chat
        if keep:
            print(f"⚠️ --keep：测试记忆集 #{set_id} 保留，记得手工清理")
        else:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM memory_items WHERE set_id=%s", (set_id,))
                    await cur.execute("DELETE FROM memory_customers WHERE set_id=%s", (set_id,))
                    await cur.execute("DELETE FROM memory_cleanup_log WHERE set_id=%s", (set_id,))
                await conn.commit()
            await memory_service.delete_set(set_id)
            print("🧹 已清理测试数据")
        pool.close()
        await pool.wait_closed()

    print("=" * 60)
    print(f"通过 {len(PASSED)} / 失败 {len(FAILED)}")
    for n in FAILED:
        print(f"  ❌ {n}")
    return 1 if FAILED else 0


sys.exit(asyncio.run(main()))
