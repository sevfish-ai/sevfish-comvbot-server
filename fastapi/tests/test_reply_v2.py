"""
自动回复 v2 核心逻辑单元测试（不依赖数据库/LLM）：
- 策略配置归一化 / 旧配置迁移 / 模式摘要
- 队列 key 构造解析 / 类型配置回退 / 追踪@状态
- 合并窗口入队与重置（计时器引用竞态回归）
"""

import asyncio
import time

import pytest

from app.services.aivbot_service import AivbotService
from app.services.robot_service import (
    normalize_config,
    _config_from_legacy,
    _mode_summary,
)


# ── 策略配置 ─────────────────────────────────────────────

def test_normalize_config_defaults():
    cfg = normalize_config({})
    assert cfg["4"]["mode"] == "reply"       # 内部私聊
    assert cfg["2"]["mode"] == "reply"       # 外部私聊
    assert cfg["1"]["mode"] == "at_only"     # 外部群
    assert cfg["3"]["mode"] == "at_only"     # 内部群
    assert cfg["1"]["merge_window"] == 4
    assert cfg["3"]["track_duration"] == 180
    assert "track_duration" not in cfg["4"]  # 私聊无追踪时长


def test_normalize_config_invalid_values_fallback():
    cfg = normalize_config(
        {"4": {"mode": "bogus", "merge_window": 0, "track_duration": 99},
         "3": {"mode": "track_at", "merge_window": -5, "track_duration": -1},
         "1": {"mode": "reply_all", "merge_window": 10, "track_duration": 60}}
    )
    # 非法模式/窗口回退默认
    assert cfg["4"]["mode"] == "reply"
    assert cfg["4"]["merge_window"] == 4
    assert "track_duration" not in cfg["4"]
    # 群聊合法值保留，非法值回退
    assert cfg["3"]["mode"] == "track_at"
    assert cfg["3"]["merge_window"] == 4
    assert cfg["3"]["track_duration"] == 180
    assert cfg["1"]["mode"] == "reply_all"
    assert cfg["1"]["merge_window"] == 10
    assert cfg["1"]["track_duration"] == 60


def test_config_from_legacy_mapping():
    # reply_all=1 → 全部回复
    cfg = _config_from_legacy(1, 30)
    assert cfg["1"]["mode"] == "reply_all" and cfg["3"]["mode"] == "reply_all"
    assert cfg["2"]["mode"] == "reply" and cfg["4"]["mode"] == "reply"
    assert cfg["1"]["merge_window"] == 30  # 冷却秒数作为窗口初值
    # reply_all=0 → 私聊回复 / 群聊仅@
    cfg0 = _config_from_legacy(0, 10)
    assert cfg0["1"]["mode"] == "at_only" and cfg0["3"]["mode"] == "at_only"
    assert cfg0["2"]["mode"] == "reply" and cfg0["4"]["mode"] == "reply"
    # reply_all=-1 → 全部不回复
    cfgm = _config_from_legacy(-1, 10)
    assert all(c["mode"] == "silent" for c in cfgm.values())


def test_normalize_config_member_blocklist():
    # 默认无黑名单
    cfg = normalize_config({})
    assert cfg["member_blocklist"] == []
    # list 输入原样归一
    cfg = normalize_config({"member_blocklist": ["客服助理", " 张三 "], "3": {"mode": "at_only"}})
    assert cfg["member_blocklist"] == ["客服助理", "张三"]
    # 字符串按逗号拆分、去空项
    cfg = normalize_config({"member_blocklist": "客服助理, 张三,,李四"})
    assert cfg["member_blocklist"] == ["客服助理", "张三", "李四"]
    # 非法值回退空列表
    cfg = normalize_config({"member_blocklist": 123})
    assert cfg["member_blocklist"] == []
    # 非 dict 输入回退空列表
    assert normalize_config(None)["member_blocklist"] == []


def test_mode_summary():
    assert _mode_summary(normalize_config({})) == 1                      # 私聊=回复 → 有回复
    all_silent = {"1": {"mode": "silent"}, "2": {"mode": "silent"},
                  "3": {"mode": "silent"}, "4": {"mode": "silent"}}
    assert _mode_summary(all_silent) == -1
    has_reply = {"1": {"mode": "reply_all"}, "2": {"mode": "silent"},
                 "3": {"mode": "silent"}, "4": {"mode": "silent"}}
    assert _mode_summary(has_reply) == 1
    only_at = {"1": {"mode": "at_only"}, "2": {"mode": "silent"},
               "3": {"mode": "at_only"}, "4": {"mode": "silent"}}
    assert _mode_summary(only_at) == 0
    assert _mode_summary({}) == 0


# ── 队列与追踪状态 ───────────────────────────────────────

def test_queue_key_roundtrip():
    svc = AivbotService()
    # 私聊：无发言者
    assert svc._queue_key("001", "好友A") == "001|好友A"
    assert svc._parse_queue_key("001|好友A") == ("001", "好友A", "")
    # 群聊：聊天室+发言者
    key = svc._queue_key("001", "群A", "张三")
    assert key == "001|群A|张三"
    assert svc._parse_queue_key(key) == ("001", "群A", "张三")
    # 发言者名含 | 也能还原（用 join 兜底）
    key2 = svc._queue_key("001", "群A", "张|三")
    assert svc._parse_queue_key(key2)[2] == "张|三"


def test_type_config_fallback():
    svc = AivbotService()
    # 未绑定策略组 → 默认配置
    assert svc._type_config(None, 3)["mode"] == "at_only"
    assert svc._type_config(None, 4)["mode"] == "reply"
    assert svc._type_config(None, 1)["merge_window"] == 4
    # 未知类型 → silent 兜底
    assert svc._type_config(None, 99)["mode"] == "silent"


def test_track_state():
    svc = AivbotService()
    assert svc._is_tracked("群A", "张三") is False
    assert svc._is_tracked("群A", "") is False  # 无发言者不算追踪
    svc._track_state[("群A", "张三")] = time.time() + 60
    assert svc._is_tracked("群A", "张三") is True
    svc._track_state[("群A", "张三")] = time.time() - 1
    assert svc._is_tracked("群A", "张三") is False


# ── 合并窗口入队/重置（asyncio）───────────────────────────

def test_enqueue_timer_reset_and_processing_defer():
    svc = AivbotService()

    async def main():
        q = {"msgs": [], "timer": None, "processing": False, "room": "群A",
             "room_type": 3, "cfg": {"mode": "reply_all", "merge_window": 600},
             "retries": 0}
        svc._reply_queues["k"] = q
        # 入队 → 启动计时器（sleep(0) 让任务真正进入 sleep，贴合生产时序）
        svc._enqueue("k", "群A", 3, q["cfg"], {"text": "hi", "sender": 0})
        await asyncio.sleep(0)
        assert q["timer"] is not None
        first = q["timer"]
        # 再入队 → 重置（新计时器对象，旧任务已取消）
        svc._enqueue("k", "群A", 3, q["cfg"], {"text": "hi2", "sender": 0})
        await asyncio.sleep(0)
        assert q["timer"] is not first
        # 处理中 → 只入队不重置
        q["processing"] = True
        before = q["timer"]
        svc._enqueue("k", "群A", 3, q["cfg"], {"text": "hi3", "sender": 0})
        assert len(q["msgs"]) == 3  # hi + hi2 + hi3 全部积压
        assert q["timer"] is before
        # 清理：取消当前计时器并等待其收尾（竞态回归：取消后引用必须清空）
        cur = q["timer"]
        cur.cancel()
        await asyncio.sleep(0)  # 让取消处理执行（旧任务已启动，except 才会运行）
        try:
            await cur
        except asyncio.CancelledError:
            pass
        assert q["timer"] is None

    asyncio.run(main())


def test_route_auto_reply_blocklist_skips_enqueue():
    """黑名单命中的成员消息不入队（不调 LLM）；未命中正常入队"""
    svc = AivbotService()

    class _StubStrategy:
        config = normalize_config({"member_blocklist": ["客服助理"],
                                   "3": {"mode": "reply_all"}})

    class _StubRobot:
        auto_reply = True

    async def main():
        async def _strategy(cid):
            return _StubStrategy()

        async def _robot(cid):
            return _StubRobot()

        svc._get_strategy_cached = _strategy  # type: ignore
        svc._get_robot_cached = _robot  # type: ignore
        # 群聊：黑名单成员 + 普通成员各一条
        await svc._route_auto_reply(
            client_id="001", room="群A", room_type=3,
            new_texts=[
                {"text": "hello", "sender": 0, "speaker": "客服助理"},
                {"text": "hi", "sender": 0, "speaker": "张三"},
            ],
        )
        keys = list(svc._reply_queues.keys())
        assert keys == ["001|群A|张三"]  # 黑名单成员没有队列
        # 清理计时器，避免挂起
        for q in svc._reply_queues.values():
            if q["timer"]:
                q["timer"].cancel()
        svc._reply_queues.clear()

    asyncio.run(main())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
