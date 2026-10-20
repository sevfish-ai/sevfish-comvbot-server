"""
智能体「调用工作流 / 设备指令」能力的单元测试（纯逻辑，不依赖 MySQL / 网络）

覆盖：
- 指令标签/参数契约与 CMD_OPTIONS 的完整性（回归 219/220/225 缺失）
- 测试合成 robot_id 长度边界（回归 agent_conversations.robot_id VARCHAR(10) 修复）
- _build_tools 只注入已授权的工作流/设备指令工具
- _execute_device_command_tool 模拟模式只校验、不真实下发

运行：python -m pytest tests/test_agent_tools.py -v
"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import importlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routes.workflows import CMD_OPTIONS, CMD_PARAM_INFO

# 注意：app.services.__init__ 将 agent_service 暴露为单例实例，遮蔽了模块名，
# 必须用 importlib 显式导入模块本身才能访问 _CMD_LABELS / _CMD_PARAM_SCHEMA / AgentService
agent_svc_mod = importlib.import_module("app.services.agent_service")


def test_cmd_labels_complete():
    """CMD_OPTIONS 全部指令都应有中文标签（_CMD_LABELS）"""
    labels = agent_svc_mod._CMD_LABELS
    missing = [c for c, _ in CMD_OPTIONS if int(c) not in labels]
    assert not missing, f"缺少中文标签的指令: {missing}"


def test_cmd_param_schema_covers_all():
    """CMD_OPTIONS 全部指令都应有参数契约（_CMD_PARAM_SCHEMA）"""
    schema = agent_svc_mod._CMD_PARAM_SCHEMA
    missing = [c for c, _ in CMD_OPTIONS if int(c) not in schema]
    assert not missing, f"缺少参数契约的指令: {missing}"


def test_cmd_param_info_covers_all():
    """CMD_OPTIONS 全部指令都应有表单定义（routes/workflows.CMD_PARAM_INFO）"""
    missing = [c for c, _ in CMD_OPTIONS if c not in CMD_PARAM_INFO]
    assert not missing, f"缺少表单定义的指令: {missing}"


def test_test_robot_id_fits_column():
    """测试入口的合成 robot_id 必须 ≤10 字符（agent_conversations.robot_id VARCHAR(10)）"""
    for agent_id in (1, 9, 99, 123456, 999999):
        rid = f"test{agent_id}"
        assert len(rid) <= 10, f"agent_id={agent_id} 生成 {rid} 超出 VARCHAR(10)"


def _agent(**kw):
    defaults = dict(id=1, enable_tools=True)
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_build_tools_injects_authorized_tools():
    """授权了工作流 1 与指令 501/503 时，应注入对应工具"""
    svc = agent_svc_mod.AgentService()
    wf = SimpleNamespace(id=1, name="测试工作流", description="", input_schema=[], timeout_sec=60, enabled=True)
    with patch.object(svc, "get_agent_workflows", new=AsyncMock(return_value=[1])), \
         patch.object(svc, "get_agent_commands", new=AsyncMock(return_value=[501, 503])), \
         patch.object(agent_svc_mod.table_service, "get_tables_of_agent",
                      new=AsyncMock(return_value=[])), \
         patch("app.services.workflow_service.workflow_service.list_workflows",
               new=AsyncMock(return_value=[wf])):
        tools = asyncio.run(svc._build_tools(_agent(id=1)))
    names = [t["function"]["name"] for t in tools]
    assert "run_workflow_1" in names, f"缺少 run_workflow_1: {names}"
    assert "device_command_501" in names, f"缺少 device_command_501: {names}"
    assert "device_command_503" in names, f"缺少 device_command_503: {names}"
    # 不应注入未授权的其他工作流/指令工具
    extra_wf = [n for n in names if n.startswith("run_workflow_") and n != "run_workflow_1"]
    extra_cmd = [n for n in names if n.startswith("device_command_") and n not in ("device_command_501", "device_command_503")]
    assert not extra_wf and not extra_cmd, f"注入了未授权工具: {extra_wf + extra_cmd}"


def test_build_tools_omits_unauthorized():
    """无任何授权时，不应注入工作流/设备指令工具"""
    svc = agent_svc_mod.AgentService()
    with patch.object(svc, "get_agent_workflows", new=AsyncMock(return_value=[])), \
         patch.object(svc, "get_agent_commands", new=AsyncMock(return_value=[])), \
         patch.object(agent_svc_mod.table_service, "get_tables_of_agent",
                      new=AsyncMock(return_value=[])):
        tools = asyncio.run(svc._build_tools(_agent(id=2)))
    names = [t["function"]["name"] for t in tools]
    assert not any(n.startswith(("run_workflow_", "device_command_")) for n in names), names


def test_execute_device_command_simulate_does_not_send():
    """模拟模式：只校验不下发，绝不调用 send_command"""
    svc = agent_svc_mod.AgentService()
    with patch.object(svc, "get_agent_commands", new=AsyncMock(return_value=[503])), \
         patch("app.services.aivbot_service.aivbot_service") as wt_svc:
        result = asyncio.run(svc._execute_device_command_tool(
            {"cmd_type": 503}, agent_id=1, robot_id="768870", simulate=True))
    assert "模拟" in result and "校验通过" in result
    wt_svc.send_command.assert_not_called()


def test_execute_device_command_real_sends():
    """真实模式：授权且参数合法时调用 send_command，并返回设备执行结果"""
    svc = agent_svc_mod.AgentService()
    with patch.object(svc, "get_agent_commands", new=AsyncMock(return_value=[503])), \
         patch("app.services.aivbot_service.aivbot_service") as wt_svc, \
         patch("app.services.workflow_engine.workflow_engine._wait_any_result",
               new=AsyncMock(return_value={"my_info": {"name": "张三"}})):
        wt_svc.send_command = AsyncMock(return_value=True)
        result = asyncio.run(svc._execute_device_command_tool(
            {"cmd_type": 503}, agent_id=1, robot_id="768870", simulate=False))
    wt_svc.send_command.assert_called_once()
    assert "执行结果" in result
    assert "张三" in result


def test_execute_device_command_real_wait_timeout():
    """真实模式：下发成功但等待设备结果超时，返回超时提示而非'已成功'"""
    svc = agent_svc_mod.AgentService()
    with patch.object(svc, "get_agent_commands", new=AsyncMock(return_value=[503])), \
         patch("app.services.aivbot_service.aivbot_service") as wt_svc, \
         patch("app.services.workflow_engine.workflow_engine._wait_any_result",
               new=AsyncMock(return_value={"error": "等待设备结果超时（15s，key=my_info）"})):
        wt_svc.send_command = AsyncMock(return_value=True)
        result = asyncio.run(svc._execute_device_command_tool(
            {"cmd_type": 503}, agent_id=1, robot_id="768870", simulate=False))
    assert "超时" in result
    assert "已下发" in result


def test_execute_device_command_unauthorized():
    """未授权指令：返回错误提示且不调用 send_command"""
    svc = agent_svc_mod.AgentService()
    with patch.object(svc, "get_agent_commands", new=AsyncMock(return_value=[])), \
         patch("app.services.aivbot_service.aivbot_service") as wt_svc:
        result = asyncio.run(svc._execute_device_command_tool(
            {"cmd_type": 503}, agent_id=1, robot_id="768870", simulate=True))
    assert "未获授权" in result
    wt_svc.send_command.assert_not_called()


def test_execute_workflow_tool_str_output():
    """回归：output 节点返回字符串文本时，_execute_workflow_tool 不应抛 'str' object has no attribute 'get'"""
    svc = agent_svc_mod.AgentService()
    wf_info = {"id": 1, "name": "测试", "input_schema": '[{"name": "user_text", "type": "string", "required": false, "description": ""}]'}
    with patch.object(svc, "get_agent_workflows", new=AsyncMock(return_value=[1])), \
         patch.object(svc, "get_agent", new=AsyncMock(return_value=SimpleNamespace(id=1, name="测试"))), \
         patch("app.services.workflow_service.workflow_service.get_workflow", new=AsyncMock(return_value=wf_info)), \
         patch("app.services.workflow_engine.workflow_engine.execute",
               new=AsyncMock(return_value={"status": "success", "output": "123465789123456789", "error": None})):
        result = asyncio.run(svc._execute_workflow_tool(
            {"workflow_id": 1, "user_text": "你好"}, agent_id=1, robot_id="768870", simulate=True))
    assert "执行成功" in result
    assert "123465789123456789" in result


# ── 表格创建：工作流节点 + 智能体工具 ─────────────────────────

def test_workflow_node_types_include_table_create():
    """工作流节点类型注册表应包含 table_create（创建表格）"""
    from app.models.workflow import NODE_TYPES, NODE_TYPE_NAMES
    assert "table_create" in NODE_TYPE_NAMES
    labels = dict((n[0], n[1]) for n in NODE_TYPES)
    assert labels["table_create"] == "创建表格"


def test_build_tools_table_create_needs_permission():
    """建表工具需 allow_table_create 授权（安全设计：建表与表读写相互独立）"""
    svc = agent_svc_mod.AgentService()
    with patch.object(svc, "get_agent_workflows", new=AsyncMock(return_value=[])), \
         patch.object(svc, "get_agent_commands", new=AsyncMock(return_value=[])), \
         patch.object(agent_svc_mod.table_service, "get_tables_of_agent",
                      new=AsyncMock(return_value=[])):
        # 有建表权、无表授权 → 只注入 table_create，不注入读写
        tools = asyncio.run(svc._build_tools(_agent(id=3, allow_table_create=True)))
    names = [t["function"]["name"] for t in tools]
    assert "table_create" in names, f"缺少 table_create: {names}"
    assert "table_query" not in names and "table_insert" not in names, names
    # 无建表权、无表授权 → 不注入任何表格工具
    with patch.object(svc, "get_agent_workflows", new=AsyncMock(return_value=[])), \
         patch.object(svc, "get_agent_commands", new=AsyncMock(return_value=[])), \
         patch.object(agent_svc_mod.table_service, "get_tables_of_agent",
                      new=AsyncMock(return_value=[])):
        tools2 = asyncio.run(svc._build_tools(_agent(id=4)))
    names2 = [t["function"]["name"] for t in tools2]
    assert not any(n.startswith("table_") for n in names2), names2


def test_execute_table_create_grants_agent():
    """table_create 工具：创建成功后自动授权给当前智能体（读写）"""
    from app.services.table_service import table_service as table_svc
    svc = agent_svc_mod.AgentService()
    create_mock = AsyncMock(return_value=99)
    grant_mock = AsyncMock()
    with patch.object(svc, "get_agent", new=AsyncMock(
            return_value=SimpleNamespace(id=7, allow_table_create=True))), \
         patch.object(table_svc, "create_table", create_mock), \
         patch.object(table_svc, "set_agent_grants", grant_mock):
        result = asyncio.run(svc._execute_table_tool(
            "table_create",
            {"name": "客户名单", "fields": [{"name": "name", "type": "varchar", "label": "姓名"}]},
            agent_id=7,
        ))
    assert "表格创建成功" in result and "tbl_99" in result
    create_mock.assert_awaited_once()
    grant_mock.assert_awaited_once_with(99, {7: 1})


def test_execute_table_create_missing_fields():
    """table_create 工具：缺少字段定义时返回错误提示，不创建"""
    from app.services.table_service import table_service as table_svc
    svc = agent_svc_mod.AgentService()
    create_mock = AsyncMock()
    with patch.object(svc, "get_agent", new=AsyncMock(
            return_value=SimpleNamespace(id=7, allow_table_create=True))), \
         patch.object(table_svc, "create_table", create_mock):
        result = asyncio.run(svc._execute_table_tool(
            "table_create", {"name": "客户名单", "fields": []}, agent_id=7
        ))
    assert "fields" in result
    create_mock.assert_not_awaited()


def test_execute_table_create_unauthorized():
    """table_create 工具：未授权 allow_table_create 时拒绝创建"""
    svc = agent_svc_mod.AgentService()
    with patch.object(svc, "get_agent", new=AsyncMock(
            return_value=SimpleNamespace(id=7, allow_table_create=False))):
        result = asyncio.run(svc._execute_table_tool(
            "table_create", {"name": "客户名单", "fields": [{"name": "a", "type": "varchar", "label": "A"}]},
            agent_id=7,
        ))
    assert "未获授权创建表格" in result


def test_workflow_table_create_node():
    """工作流 table_create 节点：创建表格并自动授权给绑定智能体"""
    from app.services.table_service import table_service as table_svc
    from app.services.workflow_engine import workflow_engine
    create_mock = AsyncMock(return_value=12)
    grant_mock = AsyncMock()
    with patch.object(table_svc, "create_table", create_mock), \
         patch.object(table_svc, "set_agent_grants", grant_mock):
        result, ok = asyncio.run(workflow_engine._node_table_create(
            {"name": "测试表", "fields": [{"name": "a", "type": "varchar", "label": "A"}]},
            SimpleNamespace(id=5),
        ))
    assert ok
    assert result["table_id"] == 12 and result["table_key"] == "tbl_12"
    grant_mock.assert_awaited_once_with(12, {5: 1})


# ── 创建表格节点字段表单（替代 JSON 输入） ─────────────────

class _FakeForm:
    """最小化的表单替身：只支持 getlist（模拟 starlette FormData）"""

    def __init__(self, data):
        self._data = data

    def getlist(self, key):
        return self._data.get(key, [])


def test_parse_table_create_fields():
    """字段表单解析：四个数组 → 字段对象列表，空字段名行跳过"""
    from app.routes.workflows import _parse_table_create_fields
    form = _FakeForm({
        "tf_name[]": ["order_no", "amount", ""],
        "tf_type[]": ["varchar", "float", "varchar"],
        "tf_label[]": ["订单号", "金额", ""],
        "tf_required[]": ["1", "0", "1"],
    })
    fields = _parse_table_create_fields(form)
    assert fields == [
        {"name": "order_no", "type": "varchar", "label": "订单号", "required": True},
        {"name": "amount", "type": "float", "label": "金额", "required": False},
    ]


def test_parse_table_create_fields_missing_type_defaults():
    """字段表单解析：缺失 type/required 时取默认值 varchar / 否"""
    from app.routes.workflows import _parse_table_create_fields
    form = _FakeForm({"tf_name[]": ["only_name"]})
    fields = _parse_table_create_fields(form)
    assert fields == [{"name": "only_name", "type": "varchar", "label": "", "required": False}]


def test_table_fields_editor_renders_saved_fields():
    """字段表单应回填已保存的字段（含旧 JSON 字符串格式），并渲染表单控件"""
    from app.routes.workflows import _table_fields_editor
    html = _table_fields_editor({"fields": [
        {"name": "order_no", "type": "varchar", "label": "订单号", "required": True},
    ]})
    assert 'name="tf_name[]"' in html and 'value="order_no"' in html
    assert 'name="tf_type[]"' in html and 'name="tf_required[]"' in html
    assert 'selected' in html  # varchar 默认选中
    # 兼容旧 JSON 字符串格式
    html2 = _table_fields_editor({"fields": '[{"name":"a","type":"int","label":"A","required":1}]'})
    assert 'value="a"' in html2 and 'value="int"' in html2


def test_table_fields_editor_blank_default_row():
    """无已保存字段时，字段表单应渲染一个空行供填写（名称/中文名均为空值）"""
    from app.routes.workflows import _table_fields_editor
    html = _table_fields_editor({})
    assert 'name="tf_name[]"' in html
    assert html.count('value=""') == 2  # 字段名 + 中文名两个输入框为空


# ── 表格增删改查 / 联网节点：键值表单（替代 JSON 输入） ─────────────────

def test_coerce_val():
    """表单值轻量转换：数字/布尔识别，普通文本保持字符串"""
    from app.routes.workflows import _coerce_val
    assert _coerce_val("") == ""
    assert _coerce_val("100") == 100
    assert _coerce_val("-3.5") == -3.5
    assert _coerce_val("true") is True
    assert _coerce_val("FALSE") is False
    assert _coerce_val("已发货") == "已发货"
    assert _coerce_val("SO1") == "SO1"


def test_parse_kv_fields():
    """键/值 行表单解析：空键行跳过；coerce=True 时值自动转数字"""
    from app.routes.workflows import _parse_kv_fields
    form = _FakeForm({
        "ir_field[]": ["order_no", "amount", ""],
        "ir_value[]": ["SO1", "99", "x"],
    })
    assert _parse_kv_fields(form, "ir", coerce=True) == {"order_no": "SO1", "amount": 99}
    # 不转换：保持字符串
    assert _parse_kv_fields(form, "ir") == {"order_no": "SO1", "amount": "99"}


def test_parse_cond_rows():
    """条件行表单解析：空字段名行跳过，缺操作符默认 ="""
    from app.routes.workflows import _parse_cond_rows
    form = _FakeForm({
        "cq_field[]": ["status", "amount", ""],
        "cq_op[]": ["=", ">=", "like"],
        "cq_value[]": ["待发货", "100", ""],
    })
    assert _parse_cond_rows(form, "cq") == [
        {"field": "status", "op": "=", "value": "待发货"},
        {"field": "amount", "op": ">=", "value": "100"},
    ]
    form2 = _FakeForm({"cq_field[]": ["a"]})
    assert _parse_cond_rows(form2, "cq") == [{"field": "a", "op": "=", "value": ""}]


def test_kv_editor_renders_saved_data():
    """键/值 编辑器回填已保存数据（含 cond 条件与旧 JSON 字符串格式）"""
    from app.routes.workflows import _kv_editor
    html = _kv_editor("ir", "插入数据", {"row": {"order_no": "SO1", "amount": 100}}, "row")
    assert 'name="ir_field[]"' in html and 'value="order_no"' in html
    assert 'value="SO1"' in html
    html2 = _kv_editor("cq", "查询条件", {"where": [{"field": "status", "op": ">=", "value": "5"}]}, "where", kind="cond")
    assert 'name="cq_op[]"' in html2 and 'value=">="' in html2
    html3 = _kv_editor("hg", "请求头", {"headers": '{"Content-Type": "application/json"}'}, "headers")
    assert 'value="Content-Type"' in html3 and 'value="application/json"' in html3
    # 无数据时渲染一行空表单
    html4 = _kv_editor("hg", "请求头", {}, "headers")
    assert 'name="hg_field[]"' in html4 and html4.count('value=""') == 2


def test_device_command_203_carries_room_type():
    """203 发送消息要带上本会话 room_type，设备端据此校验"进入的房间类型一致"。

    起因（真机）：无自定义群名的外部群，企微头部标题是「群聊(N)」、消息列表里是
    "成员A、成员B、成员C"，两边名字天生不一致，设备端无法用标题复核是否进对房间；
    只能靠类型校验兜住"同名不同类"（群"张三" vs 联系人"张三"）。
    """
    svc = agent_svc_mod.AgentService()
    with patch.object(svc, "get_agent_commands", new=AsyncMock(return_value=[203])), \
         patch("app.services.aivbot_service.aivbot_service") as wt_svc, \
         patch("app.services.workflow_engine.workflow_engine._wait_any_result",
               new=AsyncMock(return_value={"status": "success"})):
        wt_svc.send_command = AsyncMock(return_value=True)
        asyncio.run(svc._execute_device_command_tool(
            {"cmd_type": 203, "titleList": "张三、示例科技张三、离渊",
             "receivedContent": "@离渊 收到", "textType": "1"},
            agent_id=1, robot_id="570607", simulate=False,
            tool_ctx={"room_name": "张三、示例科技张三、离渊", "room_type": 1, "speaker": "离渊",
                      "room_type_trusted": True},
        ))
    cmd_type, params = wt_svc.send_command.call_args[0][1], wt_svc.send_command.call_args[0][2]
    assert cmd_type == 203
    assert params.get("roomType") == 1, f"203 未带 roomType: {params}"
    # 私聊同样带类型（2/4），内部群带 3
    with patch.object(svc, "get_agent_commands", new=AsyncMock(return_value=[203])), \
         patch("app.services.aivbot_service.aivbot_service") as wt2, \
         patch("app.services.workflow_engine.workflow_engine._wait_any_result",
               new=AsyncMock(return_value={"status": "success"})):
        wt2.send_command = AsyncMock(return_value=True)
        asyncio.run(svc._execute_device_command_tool(
            {"cmd_type": 203, "titleList": "离渊", "receivedContent": "在的", "textType": "1"},
            agent_id=1, robot_id="570607", simulate=False,
            tool_ctx={"room_name": "离渊", "room_type": 2, "speaker": "",
                      "room_type_trusted": True},
        ))
    assert wt2.send_command.call_args[0][2].get("roomType") == 2


def test_device_error_log_302_is_stored():
    """设备端 error() 的上报（type=302）要落进「设备错误日志」，别再被 else 丢掉。

    以前 302 直接落进 `_process_item` 的 else 分支只写服务端 stdout，
    真机排查"进不去房间/发不出消息"时只能看到一句"发送失败"，看不到客户端卡在哪一步。
    """
    from app.services import aivbot_service as wt   # 单例（_process_item 在 AivbotService 上）
    store = lambda cid, key, val, **kw: stored.append((cid, key, val))
    stored = []
    original = wt._store
    wt._store = store

    async def run():
        try:
            await wt._process_item(
                {"type": 302, "log": "123: 发送失败 进入房间失败 你好"}, "570607"
            )
            await wt._process_item({"type": 302, "log": ""}, "570607")   # 空日志不要存
            await wt._process_item({"type": 999}, "570607")              # 其它类型仍然只记日志
        finally:
            wt._store = original

    asyncio.run(run())
    assert len(stored) == 1, stored
    cid, key, val = stored[0]
    assert cid == "570607" and key == "device_log"
    assert "进入房间失败" in val["log"] and val["time"]


def test_device_command_203_omits_room_type_when_guessed():
    """room_type 是"配置里猜的"（任务动作/工作流/测试页）时不下发 roomType。

    任务动作的 room_type 来自 `spec.get("room_type", 1)`（默认外部群），与消息实际要去的
    房间未必一致；拿它当校验条件会让"往私聊发消息"的任务被设备端按类型不符拒掉 —— 那是倒退。
    只有自动回复链路（room_type 来自设备 101 事件）才可信、才下发。
    """
    svc = agent_svc_mod.AgentService()
    with patch.object(svc, "get_agent_commands", new=AsyncMock(return_value=[203])),          patch("app.services.aivbot_service.aivbot_service") as wt,          patch("app.services.workflow_engine.workflow_engine._wait_any_result",
               new=AsyncMock(return_value={"status": "success"})):
        wt.send_command = AsyncMock(return_value=True)
        asyncio.run(svc._execute_device_command_tool(
            {"cmd_type": 203, "titleList": "张三", "receivedContent": "在的", "textType": "1"},
            agent_id=1, robot_id="570607", simulate=False,
            tool_ctx={"room_name": "张三", "room_type": 1},      # 没有 room_type_trusted
        ))
    assert "roomType" not in wt.send_command.call_args[0][2], wt.send_command.call_args[0][2]


def test_system_prompt_form_is_sectioned():
    """系统提示词改为「区块表单」：表单渲染区块、提交后拼回多行文本，历史纯文本不丢。"""
    from app.routes.agents import _agent_form, join_prompt, split_prompt

    text = "# 角色\n你是客服\n\n# 设备指令\n收到消息先看群名"
    sections = split_prompt(text)
    assert [t for t, _ in sections] == ["角色", "设备指令"]
    # 往返不丢内容
    assert join_prompt([t for t, _ in sections], [b for _, b in sections]) == text
    # 无标题段落（历史提示词）原样输出；空正文区块不输出
    assert join_prompt([""], ["你是客服\n第二行"]) == "你是客服\n第二行"
    assert join_prompt(["角色", "设备指令"], [" 你是客服 ", "  "]) == "# 角色\n你是客服"
    # 正文自带 `# 标题`（整段粘贴模板）时不重复加标题
    assert join_prompt(["角色"], ["# 角色\n你是客服"]) == "# 角色\n你是客服"

    html = _agent_form("/admin/agent/create", {"system_prompt": text})
    assert html.count('name="sp_body"') == html.count('name="sp_title"')  # 一一对应
    assert 'name="system_prompt"' not in html, "旧 textarea 已由区块表单取代"
    for t in ("角色", "职责", "背景", "目标", "行为规范", "处理流程", "回复要求",
              "对话示例", "知识来源", "安全边界", "设备指令", "工作流与任务",
              "数据与联网", "自定义标题"):
        assert t in html, f"预设标题缺少 {t}"
