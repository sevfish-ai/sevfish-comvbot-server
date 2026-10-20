"""回复由智能体通过设备指令（203）发出 —— 服务器不再代发。

覆盖（回归此前的"迷惑操作"）：
- 会话上下文注入：模型拿到会话名/类型/发言者，不必再调 505/502/503 找房间
- 回复规则三态：auto（自主判断）/ reply（任务必须回复）/ none（任务不要发送）
- 设备聊天路径：不再调用 aivbot_service.send_reply（服务器代发）
- 任务路径：send_reply 三态改为注入规则，也不再调用 send_reply
- 重试守卫：模型已经发出 203 后不再重投（否则客户收到两条）
- 去重：设备消息列表变短/重排不再把整批当新消息（否则同一句话被回复两次）

运行：python -m pytest tests/test_reply_via_agent.py -v
"""
import asyncio
import importlib
import json
import os
import sys
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# app.services 包把 agent_service / aivbot_service 暴露为单例实例，遮蔽了模块名，
# 必须 importlib 显式导入模块本身（同 test_agent_tools.py）
aivs_mod = importlib.import_module("app.services.aivbot_service")
ags_mod = importlib.import_module("app.services.agent_service")
ts_mod = importlib.import_module("app.services.task_service")

AivbotService = aivs_mod.AivbotService
device_send_attempted = aivs_mod.device_send_attempted


# ── 1. device_send_attempted：判断"是否可能已发出消息" ────────────────

def test_send_attempted_true_on_result():
    assert device_send_attempted([
        {"tool": "device_command_203", "result": "设备指令执行结果（203 发送消息）: {\"status\": \"success\"}"}
    ])


def test_send_attempted_true_on_timeout():
    """超时时设备可能已执行但未回报 → 保守当作已发出（宁可少重试）"""
    assert device_send_attempted([
        {"tool": "device_command_203", "result": "设备指令已下发（203 发送消息），但等待设备结果超时（15.0s）"}
    ])


def test_send_attempted_false_on_validation_error():
    for result in (
        "错误: 智能体 #1 未获授权下发指令 203（发送消息，请到「智能体管理」勾选）",
        "错误: 设备 570607 离线或连接未就绪，指令未下发",
        "错误: 指令 203（发送消息）缺少必填参数: titleList。请补充后再调用。",
        "设备指令下发异常: boom",
    ):
        assert not device_send_attempted([{"tool": "device_command_203", "result": result}]), result


def test_send_attempted_ignores_readonly_commands():
    assert not device_send_attempted([
        {"tool": "device_command_505", "result": "设备指令执行结果（505 获取最近聊天）: {}"}
    ])
    assert not device_send_attempted([{"tool": "table_insert", "result": "插入成功"}])
    assert not device_send_attempted([])
    assert not device_send_attempted(None)


# ── 2. 会话去重：列表变短/重排不再整批当新消息 ────────────────────────

def _item101(room, msgs, room_type=2, extra_titles=()):
    """构造设备上报的 type=101 消息项：msgs=[(text, sender)]

    extra_titles：设备上报的 titleList 里除房间名以外的部分（企微标题栏文本，
    如「外部群  |  群主: 张三」「123(9)」），服务端据此纠偏房间类型。
    """
    return {
        "type": 101,
        "titleList": [room, *extra_titles],
        "roomType": room_type,
        "messageList": [
            {"sender": sender, "itemMessageList": [{"feature": 1, "text": text}]}
            for text, sender in msgs
        ],
    }


def _run_ingest(service, items):
    """跑一遍 _process_item，返回每次入队的消息文本列表"""
    routed = []

    async def fake_route(client_id, room, room_type, new_texts):
        routed.append([t["text"] for t in new_texts])

    async def run():
        with patch.object(service, "_route_auto_reply", fake_route):
            for it in items:
                await service._process_item(it, "570607")
            await asyncio.sleep(0)  # 让 create_task 出来的路由跑完

    asyncio.run(run())
    return routed


def test_dedup_growing_list_only_new_tail():
    svc = AivbotService()
    routed = _run_ingest(svc, [
        _item101("离渊", [("你好", 0)]),
        _item101("离渊", [("你好", 0), ("在吗", 0)]),
    ])
    assert routed == [["你好"], ["在吗"]]


def test_dedup_shrinking_list_is_not_all_new():
    """回归：设备重读后列表变短（3→2），旧实现把整批当新消息 → 重复回复"""
    svc = AivbotService()
    routed = _run_ingest(svc, [
        _item101("离渊", [("你好", 0), ("你好吗", 0), ("你怎么不说话", 0)]),
        _item101("离渊", [("你好吗", 0), ("你怎么不说话", 0)]),
    ])
    # 首读只送最新一条；第二读没有新消息 → 不得再次入队
    assert routed == [["你怎么不说话"]]


def test_dedup_same_text_twice_both_counted():
    """同一句话连发两条是两条消息，不能被指纹去重吞掉"""
    svc = AivbotService()
    routed = _run_ingest(svc, [
        _item101("离渊", [("在吗", 0)]),                 # 建立基线
        _item101("离渊", [("在吗", 0), ("在吗", 0)]),     # 又发了一条同样的
    ])
    assert routed == [["在吗"], ["在吗"]]


def test_dedup_reordered_list_not_new():
    svc = AivbotService()
    routed = _run_ingest(svc, [
        _item101("离渊", [("A", 0), ("B", 0)]),
        _item101("离渊", [("B", 0), ("A", 0)]),
    ])
    # 只是顺序变了，没有新消息 → 不再次入队
    assert routed == [["B"]]


def test_dedup_ignores_own_messages():
    """机器人自己发的消息（sender=1）不入队"""
    svc = AivbotService()
    routed = _run_ingest(svc, [
        _item101("离渊", [("你怎么不说话", 0)]),
        _item101("离渊", [("你怎么不说话", 0), ("我在的", 1)]),          # 自己发的
        _item101("离渊", [("你怎么不说话", 0), ("我在的", 1), ("又问了", 0)]),
    ])
    assert routed == [["你怎么不说话"], ["又问了"]]


# ── 2b. 设备房间类型纠偏：外部群被报成内部群(3) ───────────────────────

def _ingest_room_types(service, items):
    """跑 _process_item，返回每次路由时的 room_type"""
    routed = []

    async def fake_route(client_id, room, room_type, new_texts):
        routed.append(room_type)

    async def run():
        with patch.object(service, "_route_auto_reply", fake_route):
            for it in items:
                await service._process_item(it, "570607")
            await asyncio.sleep(0)

    asyncio.run(run())
    return routed


def test_external_group_reported_as_internal_is_corrected():
    """回归：设备把外部群报成 room_type=3 时，按标题里的「外部群」纠正为 1。

    否则会命中策略里「内部群」那一档（常配 silent）→ 群里 @ 机器人也不回复。
    """
    svc = AivbotService()
    routed = _ingest_room_types(svc, [
        _item101("123", [("@张三 你好", 0)], room_type=3, extra_titles=("外部群  |  群主: 张三",)),
    ])
    assert routed == [1]


def test_internal_group_not_touched():
    """内部群（标题是「群名(人数)」）保持 room_type=3，不被误纠偏"""
    svc = AivbotService()
    routed = _ingest_room_types(svc, [
        _item101("内部群A", [("在吗", 0)], room_type=3, extra_titles=("内部群A(9)",)),
    ])
    assert routed == [3]


def test_room_type_correction_is_sticky():
    """纠偏只认到一次就记住：后续标题读不到（只剩房间名）时不再退回 3

    （16:39:51 那次就是标题只读到 ["123"]，没有纠偏 → 落回 silent → 消息被丢）
    """
    svc = AivbotService()
    routed = _ingest_room_types(svc, [
        _item101("123", [("一", 0)], room_type=3, extra_titles=("外部群  |  群主: 张三",)),
        _item101("123", [("一", 0), ("二", 0)], room_type=3),          # 标题读不到
    ])
    assert routed == [1, 1]


def test_external_single_chat_not_touched():
    """私聊里的「外部群」字样不参与纠偏（只看 titleList[1:]，且只在 room_type=3 时）"""
    svc = AivbotService()
    routed = _ingest_room_types(svc, [_item101("离渊", [("你好", 0)], room_type=2)])
    assert routed == [2]


def test_dedup_first_read_only_routes_newest():
    """回归：服务端重启后首次上报整窗都是历史 → 只把最后一条当"刚到"的消息。

    否则窗口里翻出的旧 @ 消息会重新开启 track_at 追踪（现象：没人 @ 也回复），
    旧消息也会被当成新消息重复处理。
    """
    svc = AivbotService()
    routed = _run_ingest(svc, [
        _item101("离渊", [("@张三 帮我查单", 0), ("在吗", 0), ("你收到我的消息了吗？", 0)])
    ])
    assert routed == [["你收到我的消息了吗？"]]


def test_dedup_after_baseline_only_real_new():
    svc = AivbotService()
    routed = _run_ingest(svc, [
        _item101("离渊", [("旧消息", 0)]),
        _item101("离渊", [("旧消息", 0), ("新消息", 0)]),
    ])
    assert routed == [["旧消息"], ["新消息"]]


# ── 2c. 旧 @ 消息不得重新开启追踪（没 @ 就不该回复） ───────────────────

def test_stale_mention_in_first_read_does_not_open_tracking():
    """完整复现 16:39 的误回复：重启后首窗含旧 @ + 一条新的无 @ 消息。

    期望：新的无 @ 消息被 track_at 丢弃（不回复），旧 @ 也不能开启追踪窗口。
    """
    called = []
    svc, strategy, fake_call = _track_at_service(called)

    async def run():
        with patch.object(svc, "_get_strategy_cached", AsyncMock(return_value=strategy)), \
             patch.object(svc, "_get_robot_cached", AsyncMock(return_value=SimpleNamespace(auto_reply=1))), \
             patch.object(svc, "_call_agent_and_reply", fake_call):
            # 首窗：历史里有旧的 @（speaker 未解析），最新一条没有 @
            await svc._process_item(
                _item101("123", [("@张三 你好，你还认识我吗", 0), ("你收到我的消息了吗？", 0)],
                         room_type=3, extra_titles=("123(9)", "外部群  |  群主: 张三")),
                "570607",
            )
            await asyncio.sleep(0)
            for k in list(svc._reply_queues):
                q = _drain(svc, k)
                await svc._process_queue(k, q)

    asyncio.run(run())
    assert called == [], "旧 @ 消息不应开启追踪窗口"


def test_fresh_mention_opens_tracking():
    """新到的 @ 消息仍应开启追踪并回复"""
    called = []
    svc, strategy, fake_call = _track_at_service(called)

    async def run():
        with patch.object(svc, "_get_strategy_cached", AsyncMock(return_value=strategy)), \
             patch.object(svc, "_get_robot_cached", AsyncMock(return_value=SimpleNamespace(auto_reply=1))), \
             patch.object(svc, "_call_agent_and_reply", fake_call):
            await svc._route_auto_reply(
                "570607", "123", 1, [{"text": "@张三 帮我查单", "sender": 0, "speaker": ""}]
            )
            q = _drain(svc, "570607|123")
            await svc._process_queue("570607|123", q)

    asyncio.run(run())
    assert len(called) == 1


# ── 3. 重试守卫：已通过设备指令发出后不再重投 ─────────────────────────

def _queue(msgs, retries=0):
    return {
        "msgs": deque(msgs),
        "timer": None,
        "processing": False,
        "room": "离渊",
        "room_type": 2,
        "cfg": {"mode": "reply", "merge_window": 4},
        "retries": retries,
    }


def _run_process_queue(q, trace_on_fail):
    svc = AivbotService()

    async def fake_call(client_id, room, speaker, room_type, batch, cfg, trace):
        trace.extend(trace_on_fail)
        raise RuntimeError("LLM 调用失败")

    async def run():
        with patch.object(svc, "_call_agent_and_reply", fake_call):
            await svc._process_queue("570607|离渊", q)

    asyncio.run(run())


def test_no_retry_after_message_already_sent():
    """模型已发出 203 后本轮抛错 → 不得重投（否则客户收到两条）"""
    q = _queue([{"text": "你怎么不说话", "sender": 0}])
    _run_process_queue(q, [{"tool": "device_command_203", "result": "设备指令执行结果（203 发送消息）: {}"}])
    assert len(q["msgs"]) == 0
    assert q["retries"] == 0


def test_retry_when_nothing_sent():
    """未发出任何消息时仍按旧规则重试（最多 2 次）"""
    q = _queue([{"text": "你怎么不说话", "sender": 0}])
    _run_process_queue(q, [{"tool": "device_command_203", "result": "错误: 设备离线或连接未就绪，指令未下发"}])
    assert len(q["msgs"]) == 1
    assert q["retries"] == 1


# ── 4. 设备聊天路径：服务器不再代发 ──────────────────────────────────

def test_call_agent_does_not_send_reply():
    svc = AivbotService()
    sent = AsyncMock(return_value=True)
    trace = []

    async def fake_generate(**kw):
        # 模拟模型自己调 203 发出，然后按规则回一份文本用于记录
        kw["trace"].append(
            {"tool": "device_command_203", "result": "设备指令执行结果（203 发送消息）: {}"}
        )
        assert kw["reply_policy"] == "auto"
        return "我在的，有什么可以帮您？"

    async def run():
        with patch.object(svc, "_get_robot_cached", AsyncMock(return_value=SimpleNamespace(agent_id=1))), \
             patch.object(ags_mod.agent_service, "get_agent", AsyncMock(return_value=SimpleNamespace(id=1, enabled=True))), \
             patch.object(ags_mod.agent_service, "generate_reply", fake_generate), \
             patch.object(aivs_mod.aivbot_service, "send_reply", sent):
            await svc._call_agent_and_reply(
                "570607", "离渊", "", 2, [{"text": "你怎么不说话", "sender": 0}],
                {"mode": "reply"}, trace,
            )

    asyncio.run(run())
    sent.assert_not_awaited()


def test_call_agent_passes_room_and_speaker():
    """会话名/发言者必须传给 generate_reply：模型靠它填 203 的 titleList"""
    svc = AivbotService()
    seen = {}

    async def fake_generate(**kw):
        seen.update(kw)
        return "收到"

    async def run():
        with patch.object(svc, "_get_robot_cached", AsyncMock(return_value=SimpleNamespace(agent_id=1))), \
             patch.object(ags_mod.agent_service, "get_agent", AsyncMock(return_value=SimpleNamespace(id=1, enabled=True))), \
             patch.object(ags_mod.agent_service, "generate_reply", fake_generate):
            await svc._call_agent_and_reply(
                "570607", "测试群", "张三", 1, [{"text": "@我 在吗", "sender": 0}],
                {"mode": "reply_all"}, [],
            )

    asyncio.run(run())
    assert seen["room_name"] == "测试群"
    assert seen["speaker"] == "张三"
    assert seen["reply_policy"] == "auto"


# ── 4b. 群聊 track_at：昵称没解析出来时不能把 @ 消息丢掉 ──────────────

def _track_at_service(called):
    svc = AivbotService()
    strategy = SimpleNamespace(
        config={"1": {"mode": "track_at", "merge_window": 4, "track_duration": 180}}
    )

    async def fake_call(client_id, room, speaker, room_type, batch, cfg, trace):
        called.append((room, speaker, [e["text"] for e in batch]))

    return svc, strategy, fake_call


def _drain(svc, key):
    """立刻处理队列（跳过 4s 合并窗口），并清掉计时器"""
    q = svc._reply_queues[key]
    if q.get("timer") is not None:
        q["timer"].cancel()
        q["timer"] = None
    return q


def test_track_at_at_mention_without_speaker_still_replies():
    """回归：设备端 nameList 为空（speaker=''）时，@了机器人也必须回复。

    旧实现 `_is_tracked` 对空 speaker 直接返回 False → @ 消息被静默丢弃。
    """
    called = []
    svc, strategy, fake_call = _track_at_service(called)

    async def run():
        with patch.object(svc, "_get_strategy_cached", AsyncMock(return_value=strategy)), \
             patch.object(svc, "_get_robot_cached", AsyncMock(return_value=SimpleNamespace(auto_reply=1))), \
             patch.object(svc, "_call_agent_and_reply", fake_call):
            await svc._route_auto_reply(
                "570607", "123", 1, [{"text": "@张三 你好，还记得我吗？", "sender": 0, "speaker": ""}]
            )
            q = _drain(svc, "570607|123")
            await svc._process_queue("570607|123", q)

    asyncio.run(run())
    assert called == [("123", "", ["@张三 你好，还记得我吗？"])]


def test_track_at_without_mention_still_dropped():
    """未 @ 的消息在 track_at 下仍应丢弃（不回复）"""
    called = []
    svc, strategy, fake_call = _track_at_service(called)

    async def run():
        with patch.object(svc, "_get_strategy_cached", AsyncMock(return_value=strategy)), \
             patch.object(svc, "_get_robot_cached", AsyncMock(return_value=SimpleNamespace(auto_reply=1))), \
             patch.object(svc, "_call_agent_and_reply", fake_call):
            await svc._route_auto_reply(
                "570607", "123", 1, [{"text": "消息收到了吗", "sender": 0, "speaker": ""}]
            )
            q = _drain(svc, "570607|123")
            await svc._process_queue("570607|123", q)

    asyncio.run(run())
    assert called == []


def test_is_tracked_room_level_fallback():
    """空 speaker → 查房间级追踪标记；有 speaker → 仍按成员隔离"""
    svc = AivbotService()
    now = time.time()
    svc._track_state[("123", "")] = now + 60
    assert svc._is_tracked("123", "") is True
    assert svc._is_tracked("123", "果冻") is False   # 成员级未登记 → 不串到别人
    svc._track_state[("123", "果冻")] = now + 60
    assert svc._is_tracked("123", "果冻") is True


# ── 5. 任务路径：三态改注入，不再代发 ────────────────────────────────

def _run_task_action(spec, ctx, reply="已处理"):
    svc = ts_mod.TaskService()
    sent = AsyncMock(return_value=True)
    seen = {}

    async def fake_generate(agent, **kw):
        seen.update(kw)
        return reply

    async def run():
        with patch.object(ts_mod, "agent_service") as mock_agent, \
             patch.object(ts_mod, "aivbot_service") as mock_aivbot:
            mock_agent.get_agent = AsyncMock(return_value=SimpleNamespace(id=1))
            mock_agent.generate_reply = fake_generate
            mock_aivbot.send_reply = sent
            task = {
                "id": 1,
                "action_type": "agent",
                "action_spec": json.dumps(spec),
                "robot_id": "R1",
            }
            out = await svc._exec_action(task, ctx)
            return out, sent

    out, sent = asyncio.run(run())
    return out, sent, seen


def test_task_agent_action_never_sends_server_side():
    out, sent, seen = _run_task_action(
        {"agent_id": 1, "instruction": "发个欢迎语", "send_reply": "source"}, {"room": "张三"}
    )
    sent.assert_not_awaited()          # 服务器不代发
    assert seen["reply_policy"] == "reply"
    assert seen["room_name"] == "张三"
    assert out == {"agent_id": 1, "reply": "已处理"}


def test_task_agent_action_custom_room_policy():
    _, sent, seen = _run_task_action(
        {"agent_id": 1, "instruction": "hi", "send_reply": "custom", "room": "客户群"},
        {"room": "触发会话"},
    )
    sent.assert_not_awaited()
    assert seen["reply_policy"] == "reply"
    assert seen["room_name"] == "客户群"


def test_task_agent_action_none_policy_is_silent():
    """send_reply=none（只干活不发消息）→ 注入"不要发送"，且服务器不发"""
    _, sent, seen = _run_task_action(
        {"agent_id": 1, "instruction": "记录一条数据", "send_reply": "none"}, {}
    )
    sent.assert_not_awaited()
    assert seen["reply_policy"] == "none"


def test_task_agent_action_empty_reply_still_fails():
    """回归：空回复仍记为任务失败（避免"已成功但没干活"的假象）"""
    svc = ts_mod.TaskService()

    async def run():
        with patch.object(ts_mod, "agent_service") as mock_agent, \
             patch.object(ts_mod, "aivbot_service"):
            mock_agent.get_agent = AsyncMock(return_value=SimpleNamespace(id=1))
            mock_agent.generate_reply = AsyncMock(return_value=None)
            task = {
                "id": 1,
                "action_type": "agent",
                "action_spec": json.dumps({"agent_id": 1, "instruction": "hi", "send_reply": "none"}),
                "robot_id": "R1",
            }
            await svc._exec_action(task, {"robot_id": "R1"})

    with pytest.raises(ValueError, match="回复生成失败"):
        asyncio.run(run())


# ── 6. 注入内容（需 MySQL）：会话上下文 + 回复规则三态 ────────────────
# 与 test_skill_trigger.py 同款做法：真实池 + 隔离 id，只拦 _chat_completion

import aiomysql  # noqa: E402

from app.config import settings  # noqa: E402

DB = dict(host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
          password=settings.MYSQL_PASSWORD, db=settings.MYSQL_DATABASE, charset="utf8mb4")
TEST_AGENT_ID = 990002
TEST_ROBOT = "zz-rva-01"


def _mysql_available():
    async def probe():
        pool = await aiomysql.create_pool(**DB, autocommit=True)
        pool.close()
        await pool.wait_closed()

    try:
        asyncio.run(probe())
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(not _mysql_available(), reason="需要本地 MySQL")


async def _prepare_db(pool):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM agent_conversations WHERE robot_id=%s", (TEST_ROBOT,))
            await cur.execute("DELETE FROM agents WHERE id=%s", (TEST_AGENT_ID,))
            await cur.execute(
                """INSERT INTO agents (id,name,description,provider,base_url,model,api_key,system_prompt,enabled)
                   VALUES (%s,'zz回复归属测试','','custom','http://127.0.0.1:1','m','k','ZZ_BASE',1)""",
                (TEST_AGENT_ID,),
            )
            await conn.commit()


async def _cleanup_db(pool):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM agent_conversations WHERE robot_id=%s", (TEST_ROBOT,))
            await cur.execute("DELETE FROM agents WHERE id=%s", (TEST_AGENT_ID,))
            await conn.commit()


@pytestmark_db
def test_injected_session_context_and_policies():
    captured = []

    async def fake_chat(agent, messages, **kw):
        captured.append("\n".join(
            m.get("content", "") for m in messages if m.get("role") == "system"
        ))
        return "好的"

    async def main():
        pool = await aiomysql.create_pool(**DB, autocommit=True)
        orig = ags_mod.agent_service._chat_completion
        try:
            ags_mod.agent_service.set_pool(pool)
            await _prepare_db(pool)
            agent = await ags_mod.agent_service.get_agent(TEST_AGENT_ID)
            ags_mod.agent_service._chat_completion = fake_chat
            await ags_mod.agent_service.generate_reply(
                agent=agent, robot_id=TEST_ROBOT, room_name="离渊", room_type=2,
                user_text="你怎么不说话", reply_policy="auto",
            )
            await ags_mod.agent_service.generate_reply(
                agent=agent, robot_id=TEST_ROBOT, room_name="客户群", room_type=1,
                user_text="报修", speaker="张三", reply_policy="reply",
            )
            await ags_mod.agent_service.generate_reply(
                agent=agent, robot_id=TEST_ROBOT, room_name="客户群", room_type=1,
                user_text="记录一下", speaker="张三", reply_policy="none",
            )
            # 群聊但设备没解析出发言者昵称：不得要求模型编造 @对象
            await ags_mod.agent_service.generate_reply(
                agent=agent, robot_id=TEST_ROBOT, room_name="匿名群", room_type=1,
                user_text="在吗", reply_policy="auto",
            )
        finally:
            ags_mod.agent_service._chat_completion = orig
            await _cleanup_db(pool)
            pool.close()
            await pool.wait_closed()

    asyncio.run(main())

    auto_sys, reply_sys, none_sys, anon_sys = captured
    # 私聊：会话名 + 类型 + 203 填法 + 自主判断
    assert "会话名称：离渊" in auto_sys
    assert "会话类型：私聊" in auto_sys
    assert "device_command_203" in auto_sys
    assert "titleList 填「离渊」" in auto_sys
    assert "不要为了找会话" in auto_sys
    assert "是否回复由你判断" in auto_sys
    # 群聊 + 必须回复：发言者 / at 规则 / 必须回复
    assert "会话名称：客户群" in reply_sys
    assert "会话类型：群聊" in reply_sys
    assert "当前发言者：张三" in reply_sys
    assert "atList" in reply_sys
    assert "本次必须回复" in reply_sys
    # none：明确不发送
    assert "本次不需要回复" in none_sys
    assert "不要调用 device_command_203" in none_sys
    assert "是否回复由你判断" not in none_sys
    # 群聊但无发言者：不要求 at，且明确禁止编造 @对象
    assert "不要编造 @对象" in anon_sys
    assert "当前发言者" not in anon_sys
