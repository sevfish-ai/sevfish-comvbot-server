"""
组织配置智能体（agents.kind='provisioner'）的工具集。

管理员在「组织管理 → AI 配置组织」页用自然语言描述需求，这个智能体直接调用
下面的 provision_* 工具，在库里创建出组织及其配套资源（智能体/表格/工作流/
知识库/任务/设备/外网账号），并汇报结果。

设计约束：
- 只创建、只追加，不删除、不停用任何已有资源；
- 每个创建动作都把新资源归属到目标组织（org_group_members）；
- 工具定义只有 provisioner 智能体拿得到（见 agent_service._build_tools），
  执行前还会再按 agent_id 复核身份（见 execute）。
"""

import json
import logging
from typing import Any, Dict, List, Optional

from ..models.agent import Agent, AgentCreateRequest
from ..models.table import TableCreateRequest
from ..models.task import TaskCreateRequest
from ..models.workflow import WorkflowCreateRequest, WorkflowNodeRequest
from .agent_service import PROVISIONER_KIND, agent_service
from .ext_user_service import ext_user_service
from .group_service import group_service
from .knowledge_service import knowledge_service
from .memory_service import memory_service
from .robot_service import robot_service
from .table_service import table_service
from .task_service import task_service
from .workflow_service import workflow_service

logger = logging.getLogger(__name__)

PROVISIONER_NAME = "组织配置智能体"
PROVISIONER_DESC = "按管理员描述直接创建组织及其配套资源（智能体/表格/工作流/知识库/任务/设备/账号）"

# 设备指令：203 = 发送消息（智能体要在企微里回话就得有它）
CMD_SEND_MESSAGE = 203

DEFAULT_PROMPT = """你是「组织配置智能体」，服务对象是本平台的管理员。你的唯一职责是：听完管理员对某个组织（客户）的需求描述，直接在这个平台的数据库里把组织及其配套资源创建出来，然后汇报结果。

## 工作方式
1. 先判断关键信息够不够：组织名称 + 这个组织是干什么的（客户是谁、要收集/回答什么、要不要定时任务）。缺关键信息就问一句（最多问一次，不要反复确认）；信息够了就直接动手，不要输出长篇方案征求同意。
2. 按需调用工具，建议顺序：组织 → 表格 → 工作流 → 知识库 → 智能体 → 任务 → 设备/账号 → 授权。
3. 每个工具都会返回新资源的 id 和名称。后续工具要用 id（例如工作流的表节点写 tbl_<表格id>、任务要 agent_id/robot_id），别猜、别编造。
4. 最后调用 provision_summary 复核该组织，再用中文汇报：建了什么（名称+id）、还差什么需要人工做（装 App、填设备号、开外网隧道等）。
5. 只创建不删除。工具返回「错误: …」就把原文告诉管理员，不要假装成功。

## 平台资源语义
- 组织（provision_org）：资源的归属容器。设备/智能体/表格/工作流/知识库/记忆集都挂在组织下，管理页面按组织切换。
- 智能体（provision_agent）：企微里的 AI 员工，负责跟客户对话。不传 LLM 配置就继承你自己（配置智能体）的；默认给它「发送消息」指令，否则它无法在企微里回复。system_prompt 决定角色与话术，要写清身份、目标、能做什么、不做什么、信息不足时怎么办。
- 表格（provision_table）：结构化收集信息（线索、工单、待答问题…）。字段 {"name","type","label","required"}，name 用英文小写字母开头，type 取 varchar/int/float/datetime/text/json。传了 agent_id 就同时授权该智能体读写，否则它用不了这张表。
- 工作流（provision_workflow）：确定性流程，最常用是「table_insert 写表 → output 输出确认语」。节点写法：
  · table_insert：params={"table":"tbl_<表格id>","row":{"列名":"{{input.参数名}}"}}
  · output：params={"text":"回答文案，可含 {{input.xxx}}"}；next_on_success 留空即流程结束
  · 模板变量必须带 input. 前缀（如 {{input.customer}}）；来源渠道可用 {{input.room_name}}
  · input_schema 声明参数：[{"name":"customer","type":"string","required":true,"description":"客户称呼"}]
  · nodes 的第一个节点是入口，next_on_success 填下一个节点的 name
  · 传了 agent_id 就把这条工作流授权给该智能体（它会多出 run_workflow_<id> 工具）
- 知识库（provision_knowledge）：给智能体查资料（产品介绍、报价口径、FAQ）。documents=[{"filename":"产品介绍.md","content":"正文"}]，只放纯文本，不要 HTML。传 agent_id 就绑定给该智能体。
- 任务（provision_task）：定时或条件触发。trigger_type=schedule 时 trigger_spec={"cron":"0 9 * * *"}（分 时 日 月 周）；action_type=agent 时 action_spec={"agent_id":N,"instruction":"要它做什么","send_reply":"none"}（结果存任务运行记录，不发消息）；action_type=workflow 时 action_spec={"workflow_id":N,"params":{…}}。定时任务必须给 robot_id（哪台设备执行）。
- 设备（provision_device）：一台装了企微客户端的安卓手机。这里只是登记拿到设备号，真正上线要人工在手机上装 App 并填这个设备号。
- 账号（provision_account）：给客户开的外网控制台账号（只读为主）。
- 授权（provision_grant）：补授权用——智能体要用哪张表、哪条工作流、哪条设备指令、哪个知识库，在这里追加（只增不减）。

## 边界
- 不创建回复策略（策略在「回复策略」页配，默认走默认策略）。
- 不删除、不停用任何已存在资源；不碰别的组织的资源。
- 报价、工期、承诺类话术不要让智能体自由发挥，写进知识库让它照抄。"""


# ── 工具定义 ────────────────────────────────────────────────

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_OBJ = {"type": "object"}


def _fn(name: str, desc: str, props: Dict[str, Any], required: List[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }


TOOL_DEFINITIONS = [
    _fn(
        "provision_org",
        "创建一个新组织（资源归属容器）。同名组织已存在时直接返回它的 id。",
        {"name": _STR, "description": _STR},
        ["name"],
    ),
    _fn(
        "provision_agent",
        "在指定组织里创建一个对话智能体（企微 AI 员工）。LLM 配置留空则继承组织配置智能体自己的。",
        {
            "org_id": _INT,
            "name": _STR,
            "description": _STR,
            "system_prompt": {"type": "string", "description": "该智能体的角色与话术（必填，建议写全）"},
            "temperature": {"type": "number"},
            "max_tokens": _INT,
            "provider": _STR,
            "base_url": _STR,
            "model": _STR,
            "api_key": _STR,
            "can_send_message": {"type": "boolean", "description": "是否允许它在企微里发消息（默认 true）"},
            "enable_http": _BOOL,
            "allow_table_create": _BOOL,
            "allow_task_create": _BOOL,
        },
        ["org_id", "name", "system_prompt"],
    ),
    _fn(
        "provision_table",
        "在指定组织里创建一张自定义表格；传 agent_id 则同时授权该智能体读写。",
        {
            "org_id": _INT,
            "name": _STR,
            "description": _STR,
            "fields": {
                "type": "array",
                "description": "字段列表 [{name,type,label,required}]，type 取 varchar/int/float/datetime/text/json",
                "items": _OBJ,
            },
            "agent_id": _INT,
            "permission": {"type": "integer", "description": "0=只读 1=读写（默认 1）"},
        },
        ["org_id", "name", "fields"],
    ),
    _fn(
        "provision_workflow",
        "在指定组织里创建一个工作流（显式节点图）；传 agent_id 则同时授权该智能体调用。",
        {
            "org_id": _INT,
            "name": _STR,
            "description": _STR,
            "nodes": {
                "type": "array",
                "description": "节点列表 [{name,node_type,params,next_on_success,next_on_fail}]，第一个节点是入口",
                "items": _OBJ,
            },
            "input_schema": {
                "type": "array",
                "description": "输入参数声明 [{name,type,required,description}]",
                "items": _OBJ,
            },
            "is_sensitive": _BOOL,
            "timeout_sec": _INT,
            "agent_id": _INT,
        },
        ["org_id", "name", "nodes"],
    ),
    _fn(
        "provision_knowledge",
        "在指定组织里创建一个本地知识库并导入文本资料；传 agent_id 则同时绑定给该智能体。",
        {
            "org_id": _INT,
            "name": _STR,
            "description": _STR,
            "documents": {
                "type": "array",
                "description": "文档列表 [{filename,content}]，纯文本",
                "items": _OBJ,
            },
            "agent_id": _INT,
            "chunk_size": _INT,
            "top_k": _INT,
        },
        ["org_id", "name", "documents"],
    ),
    _fn(
        "provision_task",
        "在指定组织里创建一个任务（定时 schedule 或条件 condition 触发）。",
        {
            "org_id": _INT,
            "name": _STR,
            "robot_id": {"type": "string", "description": "执行设备号（定时任务必填）"},
            "trigger_type": {"type": "string", "description": "schedule | condition"},
            "trigger_spec": {"type": "object", "description": '如 {"cron":"0 9 * * *"}'},
            "action_type": {"type": "string", "description": "agent | workflow"},
            "action_spec": {
                "type": "object",
                "description": 'action=agent 时 {"agent_id":N,"instruction":"…","send_reply":"none"}',
            },
            "enabled": _BOOL,
        },
        ["org_id", "name", "trigger_type", "action_type"],
    ),
    _fn(
        "provision_device",
        "登记一台企微设备（拿到设备号），可选绑定智能体与回复策略。",
        {
            "org_id": _INT,
            "device_name": _STR,
            "remark": _STR,
            "agent_id": _INT,
            "strategy_id": _INT,
        },
        ["org_id", "device_name"],
    ),
    _fn(
        "provision_account",
        "给客户开一个外网控制台账号（只读为主）。",
        {"org_id": _INT, "username": _STR, "password": _STR},
        ["org_id", "username", "password"],
    ),
    _fn(
        "provision_grant",
        "把已有的表格/工作流/知识库/设备指令追加授权给某个智能体（只增不减）。",
        {
            "agent_id": _INT,
            "workflow_ids": {"type": "array", "items": _INT},
            "command_types": {"type": "array", "items": _INT, "description": "设备指令号，如 203 发消息"},
            "table_ids": {"type": "array", "items": _INT},
            "kb_ids": {"type": "array", "items": _INT},
            "permission": {"type": "integer", "description": "表格权限 0=只读 1=读写（默认 1）"},
        },
        ["agent_id"],
    ),
    _fn(
        "provision_summary",
        "列出某个组织当前的全部资源（名称 + id），用于创建后复核与汇报。",
        {"org_id": _INT},
        ["org_id"],
    ),
]


# ── 参数取值小工具 ──────────────────────────────────────────

def _s(args: dict, key: str, default: str = "") -> str:
    v = args.get(key)
    return str(v).strip() if v is not None else default


def _i(args: dict, key: str, default: int = 0) -> int:
    try:
        return int(args.get(key) or default)
    except (TypeError, ValueError):
        return default


def _f(args: dict, key: str, default: float = 0.7) -> float:
    try:
        return float(args.get(key) if args.get(key) is not None else default)
    except (TypeError, ValueError):
        return default


def _b(args: dict, key: str, default: bool = False) -> bool:
    v = args.get(key)
    return default if v is None else bool(v)


def _dict(args: dict, key: str) -> dict:
    """取值并兼容 LLM 把对象写成 JSON 字符串的情况"""
    v = args.get(key)
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return {}
    return v if isinstance(v, dict) else {}


def _rows(args: dict, key: str) -> List[dict]:
    v = args.get(key)
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return []
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, dict)]


async def _need_org(org_id: int) -> dict:
    grp = await group_service.get_org_group(org_id)
    if not grp:
        raise ValueError(f"组织 #{org_id} 不存在，请先用 provision_org 创建")
    return grp


def _ints(args: dict, key: str) -> List[int]:
    out = []
    for x in args.get(key) or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _appended(current: List[int], add: List[int]) -> List[int]:
    """追加授权（去重、保序）——set_* 是全量覆盖，必须先读再写。"""
    out = list(current)
    for x in add:
        if int(x) not in out:
            out.append(int(x))
    return out


# ── 各工具实现 ──────────────────────────────────────────────

async def _op_org(args: dict, me: Agent) -> str:
    name = _s(args, "name")
    if not name:
        raise ValueError("缺少组织名称 name")
    for g in await group_service.list_org_groups():
        if g["name"] == name:
            return f"组织「{name}」已存在，id={g['id']}，直接使用它。"
    org_id = await group_service.create_org_group(name, _s(args, "description"))
    return f"已创建组织「{name}」 id={org_id}。"


async def _op_agent(args: dict, me: Agent) -> str:
    org_id = _i(args, "org_id")
    await _need_org(org_id)
    name = _s(args, "name")
    prompt = _s(args, "system_prompt")
    if not name:
        raise ValueError("缺少智能体名称 name")
    api_key = _s(args, "api_key") or me.api_key
    if not api_key:
        raise ValueError(
            "还没有可用的 LLM API Key：请管理员先到「组织管理 → AI 配置组织」页"
            "给组织配置智能体填好服务商与 API Key（新建的智能体会继承它）。"
        )
    agent_id = await agent_service.create_agent(
        AgentCreateRequest(
            name=name,
            description=_s(args, "description"),
            provider=_s(args, "provider") or me.provider or "custom",
            base_url=_s(args, "base_url") or me.base_url,
            model=_s(args, "model") or me.model,
            api_key=api_key,
            system_prompt=prompt,
            temperature=_f(args, "temperature"),
            max_tokens=_i(args, "max_tokens") or 8192,
            enable_tools=True,
            enable_http=_b(args, "enable_http"),
            allow_table_create=_b(args, "allow_table_create"),
            allow_task_create=_b(args, "allow_task_create"),
        )
    )
    await group_service.set_member_org_groups("agent", agent_id, [org_id])
    can_send = _b(args, "can_send_message", True)
    if can_send:
        await agent_service.set_agent_commands(agent_id, [CMD_SEND_MESSAGE])
    return (
        f"已创建智能体「{name}」 id={agent_id}（模型 {model_desc(args, me)}，"
        f"{'可发消息' if can_send else '不能发消息'}）。"
    )


def model_desc(args: dict, me: Agent) -> str:
    return _s(args, "model") or me.model or "继承"


async def _op_table(args: dict, me: Agent) -> str:
    org_id = _i(args, "org_id")
    await _need_org(org_id)
    name = _s(args, "name")
    fields = _rows(args, "fields")
    if not name:
        raise ValueError("缺少表格名称 name")
    if not fields:
        raise ValueError("缺少字段定义 fields（至少一个字段）")
    table_id = await table_service.create_table(
        TableCreateRequest(name=name, description=_s(args, "description"), fields=fields)
    )
    await group_service.set_member_org_groups("table", table_id, [org_id])
    extra = ""
    agent_id = _i(args, "agent_id")
    if agent_id:
        grants = await table_service.get_agent_grants(table_id)
        grants[int(agent_id)] = 1 if _i(args, "permission", 1) else 0
        await table_service.set_agent_grants(table_id, grants)
        extra = f"并已授权给智能体 #{agent_id}"
    cols = "、".join(str(f.get("name") or "") for f in fields)
    return f"已创建表格「{name}」 id={table_id}（表键 tbl_{table_id}，字段：{cols}）{extra}。"


async def _op_workflow(args: dict, me: Agent) -> str:
    org_id = _i(args, "org_id")
    await _need_org(org_id)
    name = _s(args, "name")
    nodes = _rows(args, "nodes")
    if not name:
        raise ValueError("缺少工作流名称 name")
    if not nodes:
        raise ValueError("缺少节点列表 nodes（至少一个节点）")
    schema = args.get("input_schema")
    if isinstance(schema, list):
        schema = json.dumps(schema, ensure_ascii=False)
    elif not isinstance(schema, str) or not schema.strip():
        schema = None
    wf_id = await workflow_service.create_workflow(
        WorkflowCreateRequest(
            name=name,
            description=_s(args, "description"),
            timeout_sec=_i(args, "timeout_sec") or 60,
            is_sensitive=_b(args, "is_sensitive"),
            input_schema=schema,
        )
    )
    entry = _s(nodes[0], "name") or "第一个节点"
    for n in nodes:
        await workflow_service.add_node(
            wf_id,
            WorkflowNodeRequest(
                node_type=_s(n, "node_type"),
                name=_s(n, "name"),
                params=_dict(n, "params"),
                next_on_success=_s(n, "next_on_success"),
                next_on_fail=_s(n, "next_on_fail"),
            ),
        )
    await group_service.set_member_org_groups("workflow", wf_id, [org_id])
    extra = ""
    agent_id = _i(args, "agent_id")
    if agent_id:
        cur = await agent_service.get_agent_workflows(int(agent_id))
        await agent_service.set_agent_workflows(int(agent_id), _appended(cur, [wf_id]))
        extra = f"，已授权给智能体 #{agent_id}"
    return f"已创建工作流「{name}」 id={wf_id}（{len(nodes)} 个节点，入口 {entry}）{extra}。"


async def _op_knowledge(args: dict, me: Agent) -> str:
    org_id = _i(args, "org_id")
    await _need_org(org_id)
    name = _s(args, "name")
    docs = _rows(args, "documents")
    if not name:
        raise ValueError("缺少知识库名称 name")
    kb_id = await knowledge_service.create_kb(
        name=name,
        kb_type="local",
        description=_s(args, "description"),
        config={"retrieval": "keyword"},
        chunk_size=_i(args, "chunk_size") or 500,
        chunk_overlap=50,
        top_k=_i(args, "top_k") or 3,
    )
    chunks = 0
    for d in docs:
        content = d.get("content")
        if not content:
            continue
        filename = _s(d, "filename") or "资料.md"
        chunks += await knowledge_service.upload_document(
            kb_id, filename, str(content).encode("utf-8")
        )
    await group_service.set_member_org_groups("knowledge", kb_id, [org_id])
    extra = ""
    agent_id = _i(args, "agent_id")
    if agent_id:
        cur = await knowledge_service.get_agent_kbs(int(agent_id))
        await knowledge_service.set_agent_kbs(int(agent_id), _appended(cur, [kb_id]))
        extra = f"，已绑定给智能体 #{agent_id}"
    return f"已创建知识库「{name}」 id={kb_id}（{len(docs)} 篇文档 / {chunks} 个片段）{extra}。"


async def _op_task(args: dict, me: Agent) -> str:
    org_id = _i(args, "org_id")
    await _need_org(org_id)
    name = _s(args, "name")
    trigger_type = _s(args, "trigger_type")
    action_type = _s(args, "action_type")
    if not name:
        raise ValueError("缺少任务名称 name")
    ok, res = await task_service.create_task(
        TaskCreateRequest(
            name=name,
            org_id=org_id,
            robot_id=_s(args, "robot_id"),
            trigger_type=trigger_type,
            trigger_spec=_dict(args, "trigger_spec"),
            action_type=action_type,
            action_spec=_dict(args, "action_spec"),
            enabled=_b(args, "enabled", True),
        )
    )
    if not ok:
        raise ValueError(str(res))
    await group_service.set_member_org_groups("task", int(res), [org_id])
    return f"已创建任务「{name}」 id={res}（{trigger_type} → {action_type}）。"


async def _op_device(args: dict, me: Agent) -> str:
    org_id = _i(args, "org_id")
    await _need_org(org_id)
    device_name = _s(args, "device_name")
    if not device_name:
        raise ValueError("缺少设备名称 device_name")
    robot_id, _token = await robot_service.register(device_name, _s(args, "remark"))
    await group_service.set_member_org_groups("device", robot_id, [org_id])
    extra = ""
    agent_id = _i(args, "agent_id")
    if agent_id:
        await agent_service.bind_agent(robot_id, int(agent_id))
        extra += f"，已绑定智能体 #{agent_id}"
    strategy_id = _i(args, "strategy_id")
    if strategy_id:
        await robot_service.update_robot_strategy(robot_id, int(strategy_id))
        extra += f"，回复策略 #{strategy_id}"
    return (
        f"已登记设备「{device_name}」 设备号={robot_id}{extra}。"
        f"还需要人工：在手机上安装客户端并把设备号填成 {robot_id}。"
    )


async def _op_account(args: dict, me: Agent) -> str:
    org_id = _i(args, "org_id")
    await _need_org(org_id)
    username = _s(args, "username")
    password = _s(args, "password")
    if not username or not password:
        raise ValueError("缺少账号 username 或密码 password")
    user_id = await ext_user_service.add_user(username, password, org_id)
    return f"已创建外网控制台账号「{username}」 id={user_id}（归属组织 #{org_id}）。"


async def _op_grant(args: dict, me: Agent) -> str:
    agent_id = _i(args, "agent_id")
    target = await agent_service.get_agent(agent_id)
    if not target:
        raise ValueError(f"智能体 #{agent_id} 不存在")
    done = []
    wf_ids = _ints(args, "workflow_ids")
    if wf_ids:
        cur = await agent_service.get_agent_workflows(agent_id)
        await agent_service.set_agent_workflows(agent_id, _appended(cur, wf_ids))
        done.append(f"工作流 {wf_ids}")
    cmd_types = _ints(args, "command_types")
    if cmd_types:
        cur = await agent_service.get_agent_commands(agent_id)
        await agent_service.set_agent_commands(agent_id, _appended(cur, cmd_types))
        done.append(f"设备指令 {cmd_types}")
    kb_ids = _ints(args, "kb_ids")
    if kb_ids:
        cur = await knowledge_service.get_agent_kbs(agent_id)
        await knowledge_service.set_agent_kbs(agent_id, _appended(cur, kb_ids))
        done.append(f"知识库 {kb_ids}")
    table_ids = _ints(args, "table_ids")
    for tid in table_ids:
        grants = await table_service.get_agent_grants(tid)
        grants[agent_id] = 1 if _i(args, "permission", 1) else 0
        await table_service.set_agent_grants(tid, grants)
    if table_ids:
        done.append(f"表格 {table_ids}")
    if not done:
        return "没有需要授权的项（workflow_ids / command_types / table_ids / kb_ids 都是空的）。"
    return f"已给智能体「{target.name}」#{agent_id} 追加授权：" + "；".join(done) + "。"


async def _op_summary(args: dict, me: Agent) -> str:
    org_id = _i(args, "org_id")
    grp = await _need_org(org_id)
    names = {
        "agent": {str(a.id): a.name for a in await agent_service.list_agents(include_provisioner=True)},
        "table": {str(t.id): t.name for t in await table_service.list_tables()},
        "workflow": {str(w.id): w.name for w in await workflow_service.list_workflows()},
        "knowledge": {str(k.id): k.name for k in await knowledge_service.list_kbs()},
        "memory": {str(m["id"]): m["name"] for m in await memory_service.list_sets()},
        "device": {str(r.robot_id): (r.device_name or r.robot_id) for r in await robot_service.list_robots()},
        "task": {str(t["id"]): t["name"] for t in await task_service.list_tasks(org_id)},
    }
    label = {
        "agent": "智能体",
        "table": "表格",
        "workflow": "工作流",
        "knowledge": "知识库",
        "memory": "记忆集",
        "device": "设备",
        "task": "任务",
    }
    lines = [f"组织「{grp['name']}」 id={org_id}（描述：{grp.get('description') or '无'}）"]
    for kind in ("agent", "table", "workflow", "knowledge", "memory", "device", "task"):
        ids = (grp.get("members") or {}).get(kind) or []
        if not ids:
            continue
        items = [f"{names[kind].get(str(i), '?')}#{i}" for i in ids]
        lines.append(f"- {label[kind]}（{len(items)}）：" + "、".join(items))
    if len(lines) == 1:
        lines.append("- 还没有任何资源")
    return "\n".join(lines)


_OPS = {
    "provision_org": _op_org,
    "provision_agent": _op_agent,
    "provision_table": _op_table,
    "provision_workflow": _op_workflow,
    "provision_knowledge": _op_knowledge,
    "provision_task": _op_task,
    "provision_device": _op_device,
    "provision_account": _op_account,
    "provision_grant": _op_grant,
    "provision_summary": _op_summary,
}


async def execute(name: str, args: dict, agent_id: int = 0) -> str:
    """执行 provision_* 工具。只有组织配置智能体本人可以调用。"""
    op = _OPS.get(name)
    if not op:
        return f"错误: 未知的配置工具「{name}」"
    me = await agent_service.get_agent(agent_id) if agent_id else None
    if not me or me.kind != PROVISIONER_KIND:
        logger.warning(f"非配置智能体 #{agent_id} 尝试调用 {name}，已拒绝")
        return "错误: 只有「组织配置智能体」可以创建组织资源"
    if not me.enabled:
        return "错误: 组织配置智能体已停用，请管理员在配置页启用"
    try:
        return await op(args or {}, me)
    except Exception as e:
        logger.warning(f"配置工具 {name} 执行失败: {e}")
        return f"错误: {e}"
