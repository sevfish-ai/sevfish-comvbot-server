"""
任务创建能力单元测试（纯逻辑，不依赖 MySQL / 网络）

覆盖：
- 工作流「创建任务」节点（_node_task_create）：复用 task_service.create_task，
  支持 trigger_spec/action_spec 为 JSON 字符串、enabled 为 "0"/"1" 字符串
- 智能体 task_create 工具：按 allow_task_create 授权注入；未授权拒绝；成功返回任务 id

运行：python -m pytest tests/test_task_create.py -v
"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import importlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# app.services.__init__ 将服务暴露为单例实例，遮蔽了模块名，必须用 importlib 显式导入模块
agent_svc_mod = importlib.import_module("app.services.agent_service")
wf_engine_mod = importlib.import_module("app.services.workflow_engine")


def _agent(**kw):
    defaults = dict(id=1, enable_tools=True, allow_task_create=False)
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ── 工作流「创建任务」节点 ──────────────────────────────


def test_node_task_create_success():
    """合法参数创建任务成功，返回 task_id；JSON 字符串 spec / enabled="0" 正确解析"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 7))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        result, ok = asyncio.run(
            engine._node_task_create(
                {
                    "name": "定时任务",
                    "trigger_type": "schedule",
                    "trigger_spec": '{"cron": "0 9 * * *"}',
                    "action_type": "workflow",
                    "action_spec": '{"workflow_id": 3}',
                    "org_id": "2",
                    "enabled": "0",
                }
            )
        )
    assert ok and result == {"task_id": 7}
    req = mock_create.await_args.args[0]
    assert req.name == "定时任务"
    assert req.org_id == 2
    assert req.trigger_spec == {"cron": "0 9 * * *"}
    assert req.action_spec == {"workflow_id": 3}
    assert req.enabled is False


def test_node_task_create_rejects_invalid():
    """create_task 返回失败时节点抛 ValueError，走失败分支"""
    engine = wf_engine_mod.WorkflowEngine()
    with patch(
        "app.services.task_service.task_service.create_task",
        new=AsyncMock(return_value=(False, "trigger_type 必须是 schedule 或 condition")),
    ):
        try:
            asyncio.run(
                engine._node_task_create(
                    {
                        "name": "x",
                        "trigger_type": "bad",
                        "trigger_spec": {},
                        "action_type": "workflow",
                        "action_spec": {},
                    }
                )
            )
            assert False, "应抛出 ValueError"
        except ValueError as e:
            assert "创建任务失败" in str(e)


def test_node_task_create_form_schedule_workflow():
    """表单模式：时间组件（每天）+ workflow 动作组装 cron"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 8))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        result, ok = asyncio.run(
            engine._node_task_create(
                {
                    "name": "表单任务",
                    "trigger_type": "schedule",
                    "trigger_time": "09:00",
                    "trigger_freq": "daily",
                    "trigger_weekdays": [],
                    "trigger_monthday": 1,
                    "trigger_interval": "",
                    "action_type": "workflow",
                    "action_workflow_id": "5",
                    "enabled": "1",
                }
            )
        )
    assert ok and result == {"task_id": 8}
    req = mock_create.await_args.args[0]
    assert req.trigger_spec == {"cron": "0 9 * * *"}
    assert req.action_spec == {"workflow_id": 5}
    assert req.enabled is True


def test_node_task_create_form_weekly_monthly():
    """表单模式：每周/每月频率组装 cron"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 8))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        # 每周一、三 08:30
        asyncio.run(engine._node_task_create({
            "name": "每周", "trigger_type": "schedule", "trigger_time": "08:30",
            "trigger_freq": "weekly", "trigger_weekdays": ["1", "3"], "trigger_monthday": 1,
            "action_type": "workflow", "action_workflow_id": "1",
        }))
        req = mock_create.await_args.args[0]
        assert req.trigger_spec == {"cron": "30 8 * * 1,3"}
        # 每月 15 号 07:15
        asyncio.run(engine._node_task_create({
            "name": "每月", "trigger_type": "schedule", "trigger_time": "07:15",
            "trigger_freq": "monthly", "trigger_weekdays": [], "trigger_monthday": 15,
            "action_type": "workflow", "action_workflow_id": "1",
        }))
        req = mock_create.await_args.args[0]
        assert req.trigger_spec == {"cron": "15 7 15 * *"}


def test_node_task_create_form_advanced_cron_fallback():
    """表单模式：未选时间时回退高级 cron / 旧数据"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 11))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(engine._node_task_create({
            "name": "高级", "trigger_type": "schedule", "trigger_time": "",
            "trigger_cron": "*/15 * * * *",
            "action_type": "workflow", "action_workflow_id": "1",
        }))
        req = mock_create.await_args.args[0]
        assert req.trigger_spec == {"cron": "*/15 * * * *"}


def test_cron_to_form_roundtrip():
    """cron_to_form 反解析：每天/每周/每月；复杂 cron 返回 None"""
    from app.services.task_service import build_cron_from_form, cron_to_form
    assert cron_to_form("0 9 * * *") == {"time": "09:00", "freq": "daily", "weekdays": [], "monthday": 1}
    assert cron_to_form("30 8 * * 1,3") == {"time": "08:30", "freq": "weekly", "weekdays": [1, 3], "monthday": 1}
    assert cron_to_form("15 7 15 * *") == {"time": "07:15", "freq": "monthly", "weekdays": [], "monthday": 15}
    assert cron_to_form("*/15 * * * *") is None
    # 组装 → 反解析 往返一致
    c = build_cron_from_form("08:30", "weekly", ["1", "3"], 1)
    assert cron_to_form(c) == {"time": "08:30", "freq": "weekly", "weekdays": [1, 3], "monthday": 1}


def test_node_task_create_form_condition_agent():
    """表单模式：扁平字段组装 condition(message_from) + agent 动作"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 9))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        result, ok = asyncio.run(
            engine._node_task_create(
                {
                    "name": "条件任务",
                    "trigger_type": "condition",
                    "trigger_cond_type": "message_from",
                    "trigger_cond_name": "张三",
                    "robot_id": "768870",
                    "action_type": "agent",
                    "action_agent_id": "2",
                    "action_instruction": "你好",
                    "action_room": "群A",
                    "action_send_reply": "1",
                }
            )
        )
    assert ok and result == {"task_id": 9}
    req = mock_create.await_args.args[0]
    assert req.trigger_spec == {"type": "message_from", "target": "张三"}
    assert req.action_spec == {
        "agent_id": 2, "instruction": "你好", "room": "群A",
        "send_reply": True,
    }


def test_node_task_create_form_friend_accepted():
    """表单模式：condition(friend_accepted) 组装 target.phone"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 10))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            engine._node_task_create(
                {
                    "name": "加好友",
                    "trigger_type": "condition",
                    "trigger_cond_type": "friend_accepted",
                    "trigger_cond_phone": "13800138000",
                    "robot_id": "768870",
                    "action_type": "workflow",
                    "action_workflow_id": "3",
                }
            )
        )
    req = mock_create.await_args.args[0]
    assert req.trigger_spec == {"type": "friend_accepted", "target": {"phone": "13800138000"}}


def test_node_task_create_form_friend_accepted_name():
    """表单模式：condition(friend_accepted) 按昵称/备注组装 target.name（企业微信显示名）"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 11))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            engine._node_task_create(
                {
                    "name": "加好友",
                    "trigger_type": "condition",
                    "trigger_cond_type": "friend_accepted",
                    "trigger_cond_name": "张三",
                    "robot_id": "768870",
                    "action_type": "workflow",
                    "action_workflow_id": "3",
                }
            )
        )
    req = mock_create.await_args.args[0]
    assert req.trigger_spec == {"type": "friend_accepted", "target": {"name": "张三"}}
    # 昵称 + 手机号可同时组装（手机号保留为选填补充）
    mock_create.reset_mock()
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            engine._node_task_create(
                {
                    "name": "加好友",
                    "trigger_type": "condition",
                    "trigger_cond_type": "friend_accepted",
                    "trigger_cond_name": "张三",
                    "trigger_cond_phone": "13800138000",
                    "robot_id": "768870",
                    "action_type": "workflow",
                    "action_workflow_id": "3",
                }
            )
        )
    req = mock_create.await_args.args[0]
    assert req.trigger_spec == {
        "type": "friend_accepted",
        "target": {"name": "张三", "phone": "13800138000"},
    }


# ── 智能体 task_create 工具 ─────────────────────────────


def test_build_tools_injects_task_create_when_authorized():
    """授权 allow_task_create 时注入 task_create 工具"""
    svc = agent_svc_mod.AgentService()
    with patch.object(svc, "get_agent_workflows", new=AsyncMock(return_value=[])), \
         patch.object(svc, "get_agent_commands", new=AsyncMock(return_value=[])), \
         patch.object(agent_svc_mod.table_service, "get_tables_of_agent",
                      new=AsyncMock(return_value=[])):
        tools = asyncio.run(svc._build_tools(_agent(id=1, allow_task_create=True)))
    names = [t["function"]["name"] for t in tools]
    assert "task_create" in names, f"缺少 task_create: {names}"


def test_build_tools_omits_task_create_without_permission():
    """未授权时不注入 task_create 工具"""
    svc = agent_svc_mod.AgentService()
    with patch.object(svc, "get_agent_workflows", new=AsyncMock(return_value=[])), \
         patch.object(svc, "get_agent_commands", new=AsyncMock(return_value=[])), \
         patch.object(agent_svc_mod.table_service, "get_tables_of_agent",
                      new=AsyncMock(return_value=[])):
        tools = asyncio.run(svc._build_tools(_agent(id=1, allow_task_create=False)))
    names = [t["function"]["name"] for t in tools]
    assert "task_create" not in names


def test_execute_task_tool_unauthorized():
    """未授权创建任务的智能体调用工具被拒绝"""
    svc = agent_svc_mod.AgentService()
    with patch.object(
        svc, "get_agent", new=AsyncMock(return_value=_agent(id=1, allow_task_create=False))
    ):
        result = asyncio.run(svc._execute_task_tool({"name": "x"}, agent_id=1))
    assert "未被授权" in result


def test_execute_task_tool_success():
    """授权后调用工具创建任务成功，返回任务 id"""
    svc = agent_svc_mod.AgentService()
    mock_create = AsyncMock(return_value=(True, 42))
    with patch.object(
        svc, "get_agent", new=AsyncMock(return_value=_agent(id=1, allow_task_create=True))
    ), patch("app.services.task_service.task_service.create_task", new=mock_create), \
         patch("app.services.agent_service.group_service.get_member_org_groups",
               new=AsyncMock(return_value=[])):
        result = asyncio.run(
            svc._execute_task_tool(
                {
                    "name": "早报",
                    "trigger_type": "schedule",
                    "trigger_spec": {"cron": "0 9 * * *"},
                    "action_type": "workflow",
                    "action_spec": {"workflow_id": 3},
                },
                agent_id=1,
            )
        )
    assert "42" in result and "早报" in result
    req = mock_create.await_args.args[0]
    assert req.trigger_spec == {"cron": "0 9 * * *"}


def test_execute_task_tool_inherits_session_context():
    """LLM 未传 robot_id/org_id 时，自动继承会话上下文（当前设备 + 当前组织）"""
    svc = agent_svc_mod.AgentService()
    mock_create = AsyncMock(return_value=(True, 43))
    with patch.object(
        svc, "get_agent", new=AsyncMock(return_value=_agent(id=1, allow_task_create=True))
    ), patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            svc._execute_task_tool(
                {
                    "name": "会话任务",
                    "trigger_type": "schedule",
                    "trigger_spec": {"cron": "0 9 * * *"},
                    "action_type": "workflow",
                    "action_spec": {"workflow_id": 3},
                },
                agent_id=1,
                tool_ctx={"robot_id": "570607", "org_id": 1, "room_name": "x"},
            )
        )
    req = mock_create.await_args.args[0]
    assert req.robot_id == "570607"
    assert req.org_id == 1


def test_execute_task_tool_explicit_args_win():
    """LLM 显式填写的 robot_id/org_id 优先于会话上下文"""
    svc = agent_svc_mod.AgentService()
    mock_create = AsyncMock(return_value=(True, 44))
    with patch.object(
        svc, "get_agent", new=AsyncMock(return_value=_agent(id=1, allow_task_create=True))
    ), patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            svc._execute_task_tool(
                {
                    "name": "显式任务",
                    "trigger_type": "schedule",
                    "trigger_spec": {"cron": "0 9 * * *"},
                    "robot_id": "999",
                    "org_id": 2,
                    "action_type": "workflow",
                    "action_spec": {"workflow_id": 3},
                },
                agent_id=1,
                tool_ctx={"robot_id": "570607", "org_id": 1},
            )
        )
    req = mock_create.await_args.args[0]
    assert req.robot_id == "999"
    assert req.org_id == 2


def test_execute_task_tool_falls_back_to_agent_org():
    """无会话上下文时，组织退化为智能体所属组织（取第一个）"""
    svc = agent_svc_mod.AgentService()
    mock_create = AsyncMock(return_value=(True, 45))
    with patch.object(
        svc, "get_agent", new=AsyncMock(return_value=_agent(id=1, allow_task_create=True))
    ), patch("app.services.task_service.task_service.create_task", new=mock_create), \
         patch("app.services.agent_service.group_service.get_member_org_groups",
               new=AsyncMock(return_value=[3])):
        asyncio.run(
            svc._execute_task_tool(
                {
                    "name": "归属任务",
                    "trigger_type": "schedule",
                    "trigger_spec": {"cron": "0 9 * * *"},
                    "action_type": "workflow",
                    "action_spec": {"workflow_id": 3},
                },
                agent_id=1,
            )
        )
    req = mock_create.await_args.args[0]
    assert req.org_id == 3
    assert req.robot_id == ""  # 无会话上下文 → 设备留空


def test_execute_tool_dispatches_task_create():
    """_execute_tool 把 task_create 分派到 _execute_task_tool"""
    svc = agent_svc_mod.AgentService()
    with patch.object(svc, "_execute_task_tool", new=AsyncMock(return_value="ok")):
        result = asyncio.run(svc._execute_tool("task_create", {"name": "x"}, agent_id=1))
    assert result == "ok"


# ── 单次定时 / 重复次数 / 相对时间偏移 ─────────────────


def test_node_task_create_form_once_absolute():
    """单次模式：datetime-local 绝对时间 → once_at 时间戳"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 20))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            engine._node_task_create(
                {
                    "name": "单次任务",
                    "trigger_type": "schedule",
                    "trigger_mode": "once",
                    "trigger_once_at": "2026-08-20T09:30",
                    "action_type": "workflow",
                    "action_workflow_id": "3",
                }
            )
        )
    req = mock_create.await_args.args[0]
    from datetime import datetime as _dt
    assert req.trigger_spec == {"once_at": int(_dt(2026, 8, 20, 9, 30).timestamp())}


def test_node_task_create_form_once_offset():
    """单次模式：相对当前时间偏移分钟 → once_at = 当前时间 ± 偏移"""
    import time as _time
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 21))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            engine._node_task_create(
                {
                    "name": "延迟任务",
                    "trigger_type": "schedule",
                    "trigger_mode": "once",
                    "trigger_once_offset_minutes": "120",
                    "action_type": "workflow",
                    "action_workflow_id": "3",
                }
            )
        )
    req = mock_create.await_args.args[0]
    expect = int(_time.time()) + 120 * 60
    assert abs(req.trigger_spec["once_at"] - expect) <= 3


def test_node_task_create_form_repeat_count_offset():
    """重复模式：重复次数 + 延迟开始偏移（offset_sec）"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 22))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            engine._node_task_create(
                {
                    "name": "限量任务",
                    "trigger_type": "schedule",
                    "trigger_time": "09:00",
                    "trigger_freq": "daily",
                    "trigger_repeat_count": "3",
                    "trigger_offset_minutes": "60",
                    "action_type": "workflow",
                    "action_workflow_id": "3",
                }
            )
        )
    req = mock_create.await_args.args[0]
    assert req.trigger_spec == {"cron": "0 9 * * *", "repeat_count": 3, "offset_sec": 3600}


def test_node_task_create_form_interval_method():
    """重复方式=固定间隔：interval_sec 生效，选择器时间被忽略，repeat_count 通用"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 23))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            engine._node_task_create(
                {
                    "name": "间隔任务",
                    "trigger_type": "schedule",
                    "repeat_method": "interval",
                    "trigger_time": "09:00",  # 不应生效
                    "trigger_freq": "daily",
                    "trigger_interval": "3600",
                    "trigger_repeat_count": "5",
                    "action_type": "workflow",
                    "action_workflow_id": "3",
                }
            )
        )
    req = mock_create.await_args.args[0]
    assert req.trigger_spec == {"interval_sec": 3600, "repeat_count": 5}


def test_node_task_create_form_cron_format_method():
    """重复方式=定时计划、定义方式=cron 表达式：直接取 trigger_cron"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 24))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            engine._node_task_create(
                {
                    "name": "cron任务",
                    "trigger_type": "schedule",
                    "repeat_method": "cron",
                    "cron_def": "format",
                    "trigger_cron": "*/15 * * * *",
                    "trigger_interval": "999",  # 不应生效
                    "action_type": "workflow",
                    "action_workflow_id": "3",
                }
            )
        )
    req = mock_create.await_args.args[0]
    assert req.trigger_spec == {"cron": "*/15 * * * *"}


def test_node_task_create_form_workflow_params():
    """节点表单：workflow 参数键值行 → params 字典（数字值保持类型）"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 25))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            engine._node_task_create(
                {
                    "name": "带参任务",
                    "trigger_type": "schedule",
                    "trigger_time": "09:00",
                    "trigger_freq": "daily",
                    "action_type": "workflow",
                    "action_workflow_id": "5",
                    "action_wf_param_key": ["text", "n"],
                    "action_wf_param_val": ["{{input.user_text}}", "3"],
                }
            )
        )
    req = mock_create.await_args.args[0]
    assert req.action_spec == {"workflow_id": 5, "params": {"text": "{{input.user_text}}", "n": 3}}


def test_node_task_create_form_workflow_empty_params():
    """节点表单：参数行全空 → 不生成 params 键"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 26))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            engine._node_task_create(
                {
                    "name": "无参任务",
                    "trigger_type": "schedule",
                    "trigger_time": "09:00",
                    "trigger_freq": "daily",
                    "action_type": "workflow",
                    "action_workflow_id": "5",
                    "action_wf_param_key": [""],
                    "action_wf_param_val": [""],
                }
            )
        )
    req = mock_create.await_args.args[0]
    assert req.action_spec == {"workflow_id": 5}


def test_node_task_create_form_agent_no_room_type():
    """节点表单：agent 动作不再产出 room_type（默认由执行端取 1=群聊）"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 28))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            engine._node_task_create(
                {
                    "name": "agent任务",
                    "trigger_type": "schedule",
                    "trigger_time": "09:00",
                    "trigger_freq": "daily",
                    "action_type": "agent",
                    "action_agent_id": "2",
                    "action_instruction": "发个问候",
                    "action_send_reply": "1",
                    "action_room": "张三",
                }
            )
        )
    req = mock_create.await_args.args[0]
    assert req.action_spec == {
        "agent_id": 2, "instruction": "发个问候", "send_reply": True, "room": "张三",
    }


def test_node_task_create_form_workflow_params_invalid():
    """节点表单：workflow 参数非合法 JSON → 报错（不静默丢弃）"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 26))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        try:
            asyncio.run(
                engine._node_task_create(
                    {
                        "name": "坏参数",
                        "trigger_type": "schedule",
                        "trigger_time": "09:00",
                        "trigger_freq": "daily",
                        "action_type": "workflow",
                        "action_workflow_id": "5",
                        "action_workflow_params": "{not json",
                    }
                )
            )
            assert False, "应抛出 ValueError"
        except ValueError as e:
            assert "JSON" in str(e)


def test_node_task_create_form_condition_expires():
    """节点表单：条件任务过期时间 → expires_at = now + N 小时"""
    engine = wf_engine_mod.WorkflowEngine()
    mock_create = AsyncMock(return_value=(True, 27))
    with patch("app.services.task_service.task_service.create_task", new=mock_create):
        asyncio.run(
            engine._node_task_create(
                {
                    "name": "限时条件",
                    "trigger_type": "condition",
                    "trigger_cond_type": "message_from",
                    "trigger_cond_name": "张三",
                    "trigger_expires_hours": "24",
                    "robot_id": "768870",
                    "action_type": "workflow",
                    "action_workflow_id": "3",
                }
            )
        )
    req = mock_create.await_args.args[0]
    assert req.trigger_spec == {"type": "message_from", "target": "张三"}
    import time as _time
    assert abs(req.expires_at - (_time.time() + 24 * 3600)) <= 3


def test_execute_task_tool_once_and_repeat():
    """智能体工具：顶层 once_at / repeat_count 并入触发配置"""
    svc = agent_svc_mod.AgentService()
    mock_create = AsyncMock(return_value=(True, 30))
    with patch.object(
        svc, "get_agent", new=AsyncMock(return_value=_agent(id=1, allow_task_create=True))
    ), patch("app.services.task_service.task_service.create_task", new=mock_create), \
         patch("app.services.agent_service.group_service.get_member_org_groups",
               new=AsyncMock(return_value=[])):
        result = asyncio.run(
            svc._execute_task_tool(
                {
                    "name": "智能体任务",
                    "trigger_type": "schedule",
                    "trigger_spec": {"cron": "0 9 * * *"},
                    "once_at": "2026-08-20T09:00",
                    "repeat_count": 5,
                    "action_type": "workflow",
                    "action_spec": {"workflow_id": 3},
                },
                agent_id=1,
            )
        )
    assert "30" in result
    req = mock_create.await_args.args[0]
    from datetime import datetime as _dt
    assert req.trigger_spec["once_at"] == int(_dt(2026, 8, 20, 9, 0).timestamp())
    assert req.trigger_spec["repeat_count"] == 5
    assert req.trigger_spec["cron"] == "0 9 * * *"


def test_next_schedule_time_raw_once_and_offset():
    """调度时间计算：once_at 未来返回、已过期返回 None；offset_sec 延迟开始"""
    svc_mod = importlib.import_module("app.services.task_service")
    now = 1_800_000_000
    # 单次：未来 → 返回该时间戳
    assert svc_mod.TaskService._next_schedule_time_raw({"once_at": now + 600}, now) == now + 600
    # 单次：已过期 → None（不再排期）
    assert svc_mod.TaskService._next_schedule_time_raw({"once_at": now - 10}, now) is None
    # 间隔 + 偏移
    assert svc_mod.TaskService._next_schedule_time_raw(
        {"interval_sec": 60, "offset_sec": 120}, now
    ) == now + 120 + 60
    # cron + 偏移：从 now+offset 之后起算
    assert svc_mod.TaskService._next_schedule_time_raw(
        {"cron": "* * * * *", "offset_sec": 30}, now
    ) == now + 60
