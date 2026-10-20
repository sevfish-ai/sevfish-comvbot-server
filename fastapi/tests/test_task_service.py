"""
任务模块单元测试（纯逻辑部分，不依赖 MySQL）

运行：python -m pytest tests/test_task_service.py -v
或：  python tests/test_task_service.py
"""
import asyncio
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.task_service import TaskService, next_cron_time
from app.services import aivbot_service


# ── cron 计算 ─────────────────────────────────────

def test_cron_daily_9am():
    base = datetime(2025, 1, 1, 8, 0, 0).timestamp()
    t = next_cron_time("0 9 * * *", base)
    assert datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") == "2025-01-01 09:00"


def test_cron_every_5_min():
    base = datetime(2025, 1, 1, 8, 2, 0).timestamp()
    t = next_cron_time("*/5 * * * *", base)
    assert datetime.fromtimestamp(t).strftime("%H:%M") == "08:05"


def test_cron_weekday_9am_skips_saturday():
    # 2025-01-04 是周六 → 下一次工作日 9 点是 01-06（周一）
    base = datetime(2025, 1, 4, 0, 0, 0).timestamp()
    t = next_cron_time("0 9 * * 1-5", base)
    assert datetime.fromtimestamp(t).strftime("%Y-%m-%d") == "2025-01-06"


def test_cron_monthly_first_day():
    base = datetime(2025, 1, 15, 0, 0, 0).timestamp()
    t = next_cron_time("0 0 1 * *", base)
    assert datetime.fromtimestamp(t).strftime("%Y-%m-%d") == "2025-02-01"


def test_cron_invalid_expr():
    assert next_cron_time("bad expr here", 0) is None
    assert next_cron_time("0 9 * *", 0) is None  # 只有 4 段


def test_cron_range_with_step():
    base = datetime(2025, 1, 1, 10, 30, 0).timestamp()
    t = next_cron_time("30 8-10 * * *", base)
    assert datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") == "2025-01-02 08:30"


# ── 条件匹配 ──────────────────────────────────────

def _eval(spec, event_type, payload):
    return asyncio.run(TaskService()._evaluate({}, spec, event_type, payload))


def test_friend_match_phone():
    spec = {"type": "friend_accepted", "target": {"phone": "13800000001", "name": "ZhangSan"}}
    ok, _ = _eval(spec, "friend_info", {"robot_id": "r1", "friend": {"phone": "13800000001", "name": "LiSi"}})
    assert ok is True


def test_friend_match_name_substring():
    spec = {"type": "friend_accepted", "target": {"name": "ZhangSan"}}
    ok, _ = _eval(spec, "friend_info", {"robot_id": "r1", "friend": {"phone": "9", "name": "ZhangSanFeng"}})
    assert ok is True


def test_friend_match_no_false_positive():
    # 好友名是目标名的子串 → 不命中（防误匹配）
    spec = {"type": "friend_accepted", "target": {"name": "ZhangSan"}}
    ok, _ = _eval(spec, "friend_info", {"robot_id": "r1", "friend": {"phone": "9", "name": "Zhang"}})
    assert ok is False
    # 完全不相关 → 不命中
    ok, _ = _eval(spec, "friend_info", {"robot_id": "r1", "friend": {"phone": "9", "name": "LiSi"}})
    assert ok is False
    # 空好友记录 → 不命中
    ok, _ = _eval(spec, "friend_info", {"robot_id": "r1", "friend": {}})
    assert ok is False


def test_friend_match_markname():
    spec = {"type": "friend_accepted", "target": {"name": "ZhangSan"}}
    ok, _ = _eval(spec, "friend_info", {"robot_id": "r1", "friend": {"phone": "9", "name": "x", "markName": "ZhangSan"}})
    assert ok is True


def test_message_from_match():
    spec = {"type": "message_from", "target": "WangWu"}
    ok, ctx = _eval(spec, "message_received", {"robot_id": "r1", "room": "WangWu", "room_type": 2})
    assert ok is True
    assert ctx.get("room") == "WangWu"
    ok, _ = _eval(spec, "message_received", {"robot_id": "r1", "room": "GroupA"})
    assert ok is False


def test_friend_accepted_message_signal_is_not_direct_fire():
    # 收到目标消息只算"信号"，不直接触发（由轮询复核），保证不误触发
    spec = {"type": "friend_accepted", "target": {"name": "ZhangSan"}}
    ok, _ = _eval(spec, "message_received", {"robot_id": "r1", "room": "ZhangSan"})
    assert ok is False


# ── 事件监听器 ────────────────────────────────────

def test_event_listener_wiring():
    got = []
    aivbot_service.add_event_listener(lambda et, p: got.append((et, p)))
    aivbot_service._emit_event("friend_info", robot_id="r1", friend={"name": "Test"})
    assert got and got[0][0] == "friend_info"
    assert got[0][1]["robot_id"] == "r1"


# ── 回归：create_task 全路径（缺失 STATE_* import 曾导致 500） ──

def test_create_task_schedule_full_path():
    """用假 pool 走 create_task 完整路径：schedule 分支引用模块级
    STATE_IDLE/STATE_WAITING 常量，曾因缺失 import 抛 NameError（点击创建任务 500）。"""
    import importlib
    import app.services.task_service as ts_mod
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    ts_mod = importlib.import_module("app.services.task_service")  # 绕过 __init__ 的单例遮蔽

    def _acm(enter_val):
        m = MagicMock()
        m.__aenter__ = AsyncMock(return_value=enter_val)
        m.__aexit__ = AsyncMock(return_value=False)
        return m

    conn, cur = MagicMock(), MagicMock()
    cur.lastrowid = 42
    cur.execute = AsyncMock()
    conn.cursor.return_value = _acm(cur)
    pool = MagicMock()
    pool.acquire.return_value = _acm(conn)

    svc = ts_mod.TaskService()
    svc._pool = pool
    req = SimpleNamespace(
        name="t", org_id=0, robot_id="", trigger_type="schedule",
        trigger_spec={"cron": "0 9 * * *"}, action_type="workflow",
        action_spec={"workflow_id": 1}, enabled=True, expires_at=0,
    )
    ok, res = asyncio.run(svc.create_task(req))
    assert ok is True and res == 42
    # 落库的 state 参数来自模块级常量（修复后为 "idle"），不是抛 NameError
    params = cur.execute.call_args[0][1]
    assert "idle" in params


# ── 回归：动作执行（_exec_action）LLM 失败必须记为任务失败 ──

def test_exec_action_agent_llm_failure_raises():
    """generate_reply 返回 None（LLM 调用异常/空内容）→ 动作抛 ValueError，
    任务将被记录为 failed 而非 success，避免"已成功但表格未写入"的假象。"""
    import importlib
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    ts_mod = importlib.import_module("app.services.task_service")
    svc = ts_mod.TaskService()
    with patch.object(ts_mod, "agent_service") as mock_agent:
        mock_agent.get_agent = AsyncMock(return_value=SimpleNamespace(id=1))
        mock_agent.generate_reply = AsyncMock(return_value=None)
        task = {
            "id": 1,
            "action_type": "agent",
            "action_spec": json.dumps({"agent_id": 1, "instruction": "hi", "send_reply": "none"}),
            "robot_id": "R1",
        }
        try:
            asyncio.run(svc._exec_action(task, {"robot_id": "R1"}))
            assert False, "LLM 失败时应抛错，不应伪装成功"
        except ValueError as e:
            assert "回复生成失败" in str(e)


def test_exec_action_agent_success_returns_reply():
    """generate_reply 正常 → out 含 reply；none 模式不下发"""
    import importlib
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    ts_mod = importlib.import_module("app.services.task_service")
    svc = ts_mod.TaskService()
    with patch.object(ts_mod, "agent_service") as mock_agent, \
         patch.object(ts_mod, "aivbot_service") as mock_aivbot:
        mock_agent.get_agent = AsyncMock(return_value=SimpleNamespace(id=1))
        mock_agent.generate_reply = AsyncMock(return_value="回复内容")
        mock_aivbot.send_reply = AsyncMock(return_value=True)
        task = {
            "id": 2,
            "action_type": "agent",
            "action_spec": json.dumps({"agent_id": 1, "instruction": "hi", "send_reply": "none"}),
            "robot_id": "R1",
        }
        out = asyncio.run(svc._exec_action(task, {"robot_id": "R1"}))
        assert out == {"agent_id": 1, "reply": "回复内容"}
        mock_aivbot.send_reply.assert_not_awaited()


# ── 回归：_handle_event 必须 await _evaluate（缺 await 曾导致条件任务永不触发） ──

def test_handle_event_awaits_evaluate_and_fires():
    """message_received 事件 → _handle_event 应 await _evaluate 并触发命中任务。
    修复前漏 await，coroutine 解包抛 TypeError 被吞，message_from 条件任务永远不触发。"""
    import importlib
    import json
    from unittest.mock import AsyncMock

    ts_mod = importlib.import_module("app.services.task_service")
    svc = ts_mod.TaskService()
    task = {
        "id": 4, "name": "收消息", "org_id": 0, "robot_id": "570607",
        "trigger_type": "condition",
        "trigger_spec": json.dumps({"type": "message_from", "target": "离渊"}),
        "action_type": "agent", "action_spec": json.dumps({"agent_id": 1, "instruction": "hi"}),
        "enabled": True, "state": "waiting", "expires_at": 0,
    }
    svc._fetch_waiting_condition_tasks = AsyncMock(return_value=[task])
    svc._fire = AsyncMock(return_value=True)
    asyncio.run(
        svc._handle_event(
            "message_received", {"robot_id": "570607", "room": "离渊", "room_type": 2}
        )
    )
    svc._fire.assert_awaited_once()
    args = svc._fire.await_args.args
    assert args[0]["id"] == 4
    assert args[2]["room"] == "离渊"


def test_handle_event_no_match_does_not_fire():
    """不匹配的事件不触发任务"""
    import importlib
    import json
    from unittest.mock import AsyncMock

    ts_mod = importlib.import_module("app.services.task_service")
    svc = ts_mod.TaskService()
    task = {
        "id": 4, "name": "收消息", "org_id": 0, "robot_id": "570607",
        "trigger_type": "condition",
        "trigger_spec": json.dumps({"type": "message_from", "target": "张三"}),
        "action_type": "agent", "action_spec": json.dumps({"agent_id": 1, "instruction": "hi"}),
        "enabled": True, "state": "waiting", "expires_at": 0,
    }
    svc._fetch_waiting_condition_tasks = AsyncMock(return_value=[task])
    svc._fire = AsyncMock(return_value=True)
    asyncio.run(
        svc._handle_event(
            "message_received", {"robot_id": "570607", "room": "离渊", "room_type": 2}
        )
    )
    svc._fire.assert_not_awaited()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERR  {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
