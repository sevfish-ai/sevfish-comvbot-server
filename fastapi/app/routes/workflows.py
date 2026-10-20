"""
命令工作流管理页面

- 工作流列表 / 创建 / 启用切换 / 删除
- 节点编排：增删改、成功/失败跳转、每类节点专属参数表单（去向全部显式声明）
- 测试运行：选设备 + 输入消息，执行整条工作流
- 执行日志：工作流级 + 节点级输入/输出/耗时
"""

import json
import logging
from datetime import datetime
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse

from ..models.workflow import (
    AUTO_LINK,
    END_TARGET,
    NODE_TYPES,
    NODE_TYPE_NAMES,
    WorkflowCreateRequest,
    WorkflowNodeRequest,
)
from ..services import (
    workflow_service,
    workflow_engine,
    agent_service,
    table_service,
    robot_service,
    group_service,
    skill_service,
)
from ..services.task_service import (
    _WEEKDAY_LABELS,
    build_cron_from_form,
    cron_to_form,
)
from ..layout import page, esc, _fmt_dt

logger = logging.getLogger(__name__)
router = APIRouter(tags=["workflows"])


def _with_msg(base: str, msg: str) -> str:
    """把 msg 拼接到重定向 URL：base 无查询串时用 ?，已有则用 &；msg 做 URL 编码。

    修复：原代码统一用 f"{base}&msg=..."，当 base 不含查询串（org_id=0）时
    会拼出 /path&msg=... 的畸形 URL，FastAPI 无法解析 path 参数返回 422。
    """
    sep = "&" if "?" in base else "?"
    return base + sep + "msg=" + quote(msg, safe="")

NODE_LABELS = {n[0]: (n[1], n[2]) for n in NODE_TYPES}

# 各节点类型的输出字段（供 {{节点名.字段}} 引用，字段名与引擎返回值一致）
NODE_OUTPUTS = {
    "llm": "content",
    "table_query": "rows(行列表), count",
    "table_create": "table_id, table_key",
    "table_insert": "id",
    "table_update": "ok",
    "table_delete": "ok",
    "http_get": "status_code, text",
    "http_post": "status_code, text",
    "device_command": "result(设备返回), cmd_type",
    "task_create": "task_id",
    "condition": "matched, actual",
    "switch": "matched, actual",
    "code": "output 键(自定义)",
    "output": "text",
}


async def _org_scoped_ids(org_id: int, member_type: str) -> Optional[set]:
    """返回指定组织下某类资源的 id 集合；org_id=0/未指定时返回 None（表示不限）"""
    if not org_id:
        return None
    return set(await group_service.get_group_org_members(org_id, member_type))


def _validate_input_schema(raw: str) -> Optional[str]:
    """
    校验并规范化工作流输入参数声明（JSON 数组）。
    数组元素支持 name / type / required / description，其他键忽略。
    返回规范化后的 JSON 字符串；空输入返回 None。
    非法 JSON 或结构错误抛出 ValueError。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"输入参数声明不是合法 JSON: {e}")
    if data is None or data == []:
        return None
    if not isinstance(data, list):
        raise ValueError("输入参数声明必须是 JSON 数组，如 [{...}]")
    out = []
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"参数声明元素必须是对象: {item}")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("参数声明缺少 name（字段名）")
        if name in seen:
            raise ValueError(f"参数名重复: {name}")
        seen.add(name)
        out.append(
            {
                "name": name,
                "type": str(item.get("type") or "string"),
                "required": bool(item.get("required", False)),
                "description": str(item.get("description") or ""),
            }
        )
    return json.dumps(out, ensure_ascii=False)


# 设备指令选项（cmd_type → 说明）
CMD_OPTIONS = [
    ("203", "发送消息"),
    ("204", "指定回复(@)"),
    ("205", "转发消息"),
    ("206", "创建群"),
    ("207", "修改群配置"),
    ("213", "修改好友备注/按手机号添加好友"),
    ("218", "推送文件"),
    ("219", "解散群"),
    ("220", "从外部群添加好友"),
    ("221", "添加待办"),
    ("222", "打卡"),
    ("223", "切换企业"),
    ("224", "推送链接"),
    ("225", "修改群成员信息"),
    ("226", "撤回消息"),
    ("233", "扫一扫"),
    ("234", "删除联系人"),
    ("501", "获取群详情"),
    ("502", "获取好友资料"),
    ("503", "获取我的信息"),
    ("505", "获取最近聊天"),
    ("506", "获取企业列表"),
    ("507", "获取全部好友"),
    ("508", "获取全部群"),
    ("509", "获取本地文件"),
]
EXPECT_TYPE_OPTIONS = [
    ("", "等待执行回执"),
    ("501", "501 群详情"),
    ("502", "502 好友资料"),
    ("503", "503 我的信息"),
    ("505", "505 最近聊天"),
    ("506", "506 企业列表"),
    ("507", "507 全部好友"),
    ("508", "508 全部群"),
]

# 各设备指令类型的参数表单定义（desc 支持 HTML，example 为 JSON 示例）
# fields 为表单字段列表：name=参数键；label=中文标签；required=必填；list=列表字段
# （填多个值用逗号分隔，引擎执行时自动拆分为数组）；kind=textarea 时渲染多行输入框；ph=占位提示。
# 必填/可选字段与 services/agent_service.py 的 _CMD_PARAM_SCHEMA 保持一致。
CMD_PARAM_INFO = {
    "203": {
        "desc": "必填 <code>titleList</code> 接收房间名（多房间逗号分隔）、<code>receivedContent</code> 消息内容、<code>textType</code> 消息类型（1=文本）；可选 <code>at</code> 提醒@、<code>atList</code> @成员。",
        "example": '{"titleList":"文件传输助手","receivedContent":"你好","textType":"1"}',
        "fields": [
            {"name": "titleList", "label": "接收房间名", "required": True, "list": True, "ph": "如 文件传输助手"},
            {"name": "receivedContent", "label": "消息内容", "required": True, "ph": "要发送的文本内容"},
            {"name": "textType", "label": "消息类型", "required": True, "ph": "1=文本"},
            {"name": "at", "label": "提醒@", "ph": "0=不提醒，1=提醒"},
            {"name": "atList", "label": "@成员", "list": True, "ph": "成员昵称"},
        ],
    },
    "204": {
        "desc": "指定回复(@)：必填 <code>titleList</code> 房间名、<code>receivedName</code> 原始发送者、<code>originalContent</code> 原始消息内容、<code>textType</code> 消息类型（1=文本）、<code>receivedContent</code> 回复内容。",
        "example": '{"titleList":"文件传输助手","receivedName":"张三","originalContent":"你好","receivedContent":"收到","textType":"1"}',
        "fields": [
            {"name": "titleList", "label": "房间名", "required": True, "list": True, "ph": "如 文件传输助手"},
            {"name": "receivedName", "label": "原始发送者", "required": True, "ph": "如 张三"},
            {"name": "originalContent", "label": "原始消息内容", "required": True, "ph": "被回复的那条消息"},
            {"name": "textType", "label": "消息类型", "required": True, "ph": "1=文本"},
            {"name": "receivedContent", "label": "回复内容", "required": True, "ph": "要回复的文本"},
        ],
    },
    "205": {
        "desc": "转发消息：必填 <code>titleList</code> 源房间名、<code>receivedName</code> 原始发送者、<code>originalContent</code> 原始消息内容、<code>textType</code> 消息类型（1=文本）、<code>nameList</code> 转发到（逗号分隔）；可选 <code>extraText</code> 附加留言。",
        "example": '{"titleList":"文件传输助手","receivedName":"张三","originalContent":"你好","textType":"1","nameList":"客户群","extraText":"请查收"}',
        "fields": [
            {"name": "titleList", "label": "源房间名", "required": True, "list": True, "ph": "如 文件传输助手"},
            {"name": "receivedName", "label": "原始发送者", "required": True, "ph": "如 张三"},
            {"name": "originalContent", "label": "原始消息内容", "required": True, "ph": "被转发的消息内容"},
            {"name": "textType", "label": "消息类型", "required": True, "ph": "1=文本"},
            {"name": "nameList", "label": "转发到", "required": True, "list": True, "ph": "目标房间/群名"},
            {"name": "extraText", "label": "附加留言", "ph": "可选"},
        ],
    },
    "206": {
        "desc": "创建群：必填 <code>groupName</code> 群名、<code>selectList</code> 成员（逗号分隔）；可选 <code>groupAnnouncement</code> 群公告、<code>groupRemark</code> 群备注、<code>groupTemplate</code> 群模板ID。",
        "example": '{"groupName":"项目群","selectList":"张三,李四","groupAnnouncement":"欢迎加入"}',
        "fields": [
            {"name": "groupName", "label": "群名", "required": True, "ph": "如 项目群"},
            {"name": "selectList", "label": "成员", "required": True, "list": True, "ph": "成员昵称"},
            {"name": "groupAnnouncement", "label": "群公告", "ph": "可选"},
            {"name": "groupRemark", "label": "群备注", "ph": "可选"},
            {"name": "groupTemplate", "label": "群模板ID", "ph": "可选"},
        ],
    },
    "207": {
        "desc": "修改群配置：必填 <code>groupName</code> 待修改群名；可选 <code>newGroupName</code> 新群名、<code>newGroupAnnouncement</code> 新群公告、<code>selectList</code> 拉人（逗号分隔）、<code>removeList</code> 踢人（逗号分隔）、<code>groupRemark</code> 群备注、<code>groupTemplate</code> 群模板ID。",
        "example": '{"groupName":"项目群","newGroupName":"项目群2","newGroupAnnouncement":"新公告","removeList":"张三"}',
        "fields": [
            {"name": "groupName", "label": "待修改群名", "required": True, "ph": "如 项目群"},
            {"name": "newGroupName", "label": "新群名", "ph": "可选"},
            {"name": "newGroupAnnouncement", "label": "新群公告", "ph": "可选"},
            {"name": "selectList", "label": "拉人", "list": True, "ph": "成员昵称，逗号分隔"},
            {"name": "removeList", "label": "踢人", "list": True, "ph": "成员昵称，逗号分隔"},
            {"name": "groupRemark", "label": "群备注", "ph": "可选"},
            {"name": "groupTemplate", "label": "群模板ID", "ph": "可选"},
        ],
    },
    "213": {
        "desc": "同指令号两种用法：<b>① 修改好友备注</b>（不传手机号）：必填 <code>friend_name</code> 好友昵称、<code>mark_name</code> 新备注名；<b>② 按手机号添加好友</b>：必填 <code>friend_phone</code> 手机号，可选 <code>friend_name</code> 姓名、<code>friend_markName</code> 备注名、<code>friend_markCorp</code> 备注企业、<code>friend_markExtra</code> 备注描述、<code>friend_leavingMsg</code> 验证附言。设备端要求好友信息放在 <code>friend</code> 对象内，发送前自动包装。",
        "example": '{"friend":{"name":"张三","markName":"销售-张"}}',
        "usages": [
            {
                "value": "remark",
                "label": "修改好友备注",
                "desc": "按好友昵称定位好友并修改备注名（不传手机号）",
                "example": '{"friend":{"name":"张三","markName":"销售-张"}}',
                "fields": [
                    {"name": "friend_name", "label": "好友昵称", "required": True, "ph": "如 张三"},
                    {"name": "mark_name", "label": "新备注名", "required": True, "ph": "如 销售-张"},
                ],
            },
            {
                "value": "add",
                "label": "按手机号添加好友",
                "desc": "通过手机号网络查找并发好友申请；备注描述、验证附言仅在添加流程中生效",
                "example": '{"friend":{"phone":"13800000000","name":"张三","markName":"客户","leavingMsg":"我是XX介绍"}}',
                "fields": [
                    {"name": "friend_phone", "label": "手机号", "required": True, "ph": "如 13800000000"},
                    {"name": "friend_name", "label": "好友姓名", "ph": "选填"},
                    {"name": "friend_markName", "label": "备注名", "ph": "选填"},
                    {"name": "friend_markCorp", "label": "备注企业", "ph": "选填"},
                    {"name": "friend_markExtra", "label": "备注描述", "ph": "选填"},
                    {"name": "friend_leavingMsg", "label": "验证附言", "ph": "选填"},
                ],
            },
        ],
    },
    "218": {
        "desc": "推送文件：必填 <code>titleList</code> 发送给（逗号分隔）、<code>objectName</code> 文件名；可选 <code>fileUrl</code> 文件URL（在线文件）、<code>fileType</code> 文件类型、<code>extraText</code> 附加留言、<code>fileBase64</code> 文件Base64（本地文件）。",
        "example": '{"titleList":"文件传输助手","objectName":"报价单.pdf","fileUrl":"https://x.com/a.pdf","extraText":"请查收"}',
        "fields": [
            {"name": "titleList", "label": "发送给", "required": True, "list": True, "ph": "房间/群名"},
            {"name": "objectName", "label": "文件名", "required": True, "ph": "如 报价单.pdf"},
            {"name": "fileUrl", "label": "文件URL", "ph": "在线文件地址"},
            {"name": "fileType", "label": "文件类型", "ph": "可选"},
            {"name": "extraText", "label": "附加留言", "ph": "可选"},
            {"name": "fileBase64", "label": "文件Base64", "kind": "textarea", "ph": "本地文件内容（Base64），与 fileUrl 二选一", "rows": 3},
        ],
    },
    "219": {
        "desc": "解散群：必填 <code>groupName</code> 待解散群名。",
        "example": '{"groupName":"项目群"}',
        "fields": [
            {"name": "groupName", "label": "待解散群名", "required": True, "ph": "如 项目群"},
        ],
    },
    "220": {
        "desc": "从外部群添加好友：必填 <code>groupName</code> 外部群名、<code>friend_name</code> 好友昵称。",
        "example": '{"groupName":"行业交流群","friend_name":"张三"}',
        "fields": [
            {"name": "groupName", "label": "外部群名", "required": True, "ph": "如 行业交流群"},
            {"name": "friend_name", "label": "好友昵称", "required": True, "ph": "如 张三"},
        ],
    },
    "221": {
        "desc": "添加待办：必填 <code>receivedContent</code> 待办内容；可选 <code>titleList</code> 成员（逗号分隔）、<code>description</code> 描述。",
        "example": '{"receivedContent":"下午开会","titleList":"张三","description":"3点"}',
        "fields": [
            {"name": "receivedContent", "label": "待办内容", "required": True, "ph": "如 下午开会"},
            {"name": "titleList", "label": "成员", "list": True, "ph": "成员昵称，逗号分隔"},
            {"name": "description", "label": "描述", "ph": "可选"},
        ],
    },
    "222": {"desc": "打卡：无需参数。", "example": "{}", "fields": []},
    "223": {
        "desc": "切换企业：必填 <code>objectName</code> 企业名称。",
        "example": '{"objectName":"某某科技有限公司"}',
        "fields": [
            {"name": "objectName", "label": "企业名称", "required": True, "ph": "如 某某科技有限公司"},
        ],
    },
    "224": {
        "desc": "推送链接：必填 <code>titleList</code> 发送给（逗号分隔）、<code>originalContent</code> 链接地址；可选 <code>objectName</code> 文章标题、<code>receivedContent</code> 文章副标题、<code>fileUrl</code> 封面图片URL、<code>extraText</code> 附加留言。",
        "example": '{"titleList":"文件传输助手","originalContent":"https://example.com/article","objectName":"文章标题"}',
        "fields": [
            {"name": "titleList", "label": "发送给", "required": True, "list": True, "ph": "房间/群名"},
            {"name": "originalContent", "label": "链接地址", "required": True, "ph": "https://..."},
            {"name": "objectName", "label": "文章标题", "ph": "可选"},
            {"name": "receivedContent", "label": "文章副标题", "ph": "可选"},
            {"name": "fileUrl", "label": "封面图片URL", "ph": "可选"},
            {"name": "extraText", "label": "附加留言", "ph": "可选"},
        ],
    },
    "225": {
        "desc": "修改群成员信息：必填 <code>groupName</code> 群名、<code>friend_name</code> 成员昵称；可选 <code>friend_markName</code> 新备注名。",
        "example": '{"groupName":"项目群","friend_name":"张三","friend_markName":"组长"}',
        "fields": [
            {"name": "groupName", "label": "群名", "required": True, "ph": "如 项目群"},
            {"name": "friend_name", "label": "成员昵称", "required": True, "ph": "如 张三"},
            {"name": "friend_markName", "label": "新备注名", "ph": "可选"},
        ],
    },
    "226": {
        "desc": "撤回消息：必填 <code>titleList</code> 房间名、<code>originalContent</code> 要撤回的消息内容、<code>textType</code> 消息类型（1=文本）。",
        "example": '{"titleList":"文件传输助手","originalContent":"说错的话","textType":"1"}',
        "fields": [
            {"name": "titleList", "label": "房间名", "required": True, "list": True, "ph": "如 文件传输助手"},
            {"name": "originalContent", "label": "要撤回的消息内容", "required": True, "ph": "原消息文本"},
            {"name": "textType", "label": "消息类型", "required": True, "ph": "1=文本"},
        ],
    },
    "233": {
        "desc": "扫一扫：必填 <code>fileUrl</code> 二维码图片URL。",
        "example": '{"fileUrl":"https://x.com/qrcode.png"}',
        "fields": [
            {"name": "fileUrl", "label": "二维码图片URL", "required": True, "ph": "https://x.com/qrcode.png"},
        ],
    },
    "234": {
        "desc": "删除联系人：必填 <code>friend_name</code> 联系人昵称。",
        "example": '{"friend_name":"张三"}',
        "fields": [
            {"name": "friend_name", "label": "联系人昵称", "required": True, "ph": "如 张三"},
        ],
    },
    "501": {
        "desc": "获取群详情：必填 <code>selectList</code> 群名。",
        "example": '{"selectList":"项目群"}',
        "fields": [
            {"name": "selectList", "label": "群名", "required": True, "list": True, "ph": "如 项目群"},
        ],
    },
    "502": {
        "desc": "获取好友资料：必填 <code>friend_name</code> 好友昵称。",
        "example": '{"friend_name":"张三"}',
        "fields": [
            {"name": "friend_name", "label": "好友昵称", "required": True, "ph": "如 张三"},
        ],
    },
    "503": {"desc": "获取我的信息：无需参数。", "example": "{}", "fields": []},
    "505": {"desc": "获取最近聊天：无需参数。", "example": "{}", "fields": []},
    "506": {"desc": "获取企业列表：无需参数。", "example": "{}", "fields": []},
    "507": {"desc": "获取全部好友：无需参数。", "example": "{}", "fields": []},
    "508": {"desc": "获取全部群：无需参数。", "example": "{}", "fields": []},
    "509": {
        "desc": "获取本地文件：必填 <code>fileUrl</code> 设备内文件路径。",
        "example": '{"fileUrl":"/storage/emulated/0/Download/a.pdf"}',
        "fields": [
            {"name": "fileUrl", "label": "设备内文件路径", "required": True, "ph": "/storage/emulated/0/Download/a.pdf"},
        ],
    },
}
CONDITION_OPS = [
    ("==", "等于"),
    ("!=", "不等于"),
    ("contains", "包含"),
    ("not_contains", "不包含"),
    ("empty", "为空"),
    ("not_empty", "不为空"),
    (">", "大于"),
    ("<", "小于"),
    (">=", "大于等于"),
    ("<=", "小于等于"),
]

# 代码节点帮助提示（渲染为节点参数区的说明卡）
_CODE_HINT = """
<div class="param-note note-dark">
<b>可用变量</b>：
<code>output</code>（结果字典，写入键值即节点输出）｜<code>result</code>（直接赋值也可）｜
<code>data</code>（输入消息）｜<code>ctx</code>（全部上下文）｜
<code>get('calc.score')</code>（按点路径取值）｜模块 <code>json / math / re / random / datetime / time</code>。<br>
<b>示例</b>：
<pre>v = get('calc.score') or 0
output['level'] = '大于1' if v > 1 else '小于等于1'
output['raw'] = v</pre>
<span style="color:#b45309">注意：代码在受限环境运行，不支持 import；需快速返回，避免死循环卡住工作流；异常会走失败分支。</span>
</div>
"""


# ════════════════════════════════════════════
#  参数表单
# ════════════════════════════════════════════


def _field(
    key,
    label,
    value="",
    ph="",
    kind="text",
    required=False,
    options=None,
    hint="",
    span=1,
    rows=3,
):
    """生成一个表单输入。options 为 select 的 [(val,label)]"""
    v = "" if value is None else str(value)
    if options is not None:
        opts = "".join(
            f'<option value="{o}"{" selected" if o == v else ""}>{l}</option>'
            for o, l in options
        )
        inp = f'<select name="params[{key}]">{opts}</select>'
    else:
        extra = ""
        if kind == "textarea":
            inp = (
                f'<textarea name="params[{key}]" rows="{rows}" placeholder="{ph}" '
                f'style="font-family:var(--mono)"{" required" if required else ""}>{v}</textarea>'
            )
        else:
            if kind == "number":
                extra = ' type="number"'
            inp = (
                f'<input type="text"{extra} name="params[{key}]" value="{v}" placeholder="{ph}" '
                f'{"required" if required else ""}>'
            )
    return (
        f'<div style="flex:1;min-width:{180 * span}px">'
        f'<label class="param-label">{label}</label>'
        f"{inp}"
        f'{"<div class=\"form-hint\">" + hint + "</div>" if hint else ""}'
        f"</div>"
    )


def _json_field(key, label, value, hint=""):
    v = value
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    if v is None:
        v = ""
    return (
        f'<div style="flex:1;min-width:220px">'
        f'<label class="param-label">{label}</label>'
        f'<textarea name="params[{key}]" rows="2" placeholder="JSON 对象，如 {{\\\\"a\\\\": 1}}" '
        f'style="font-family:var(--mono)">{esc(v)}</textarea>'
        f'{"<div class=\"form-hint\">" + hint + "</div>" if hint else ""}'
        f"</div>"
    )


def _switch_row(label, op, value, next_node, node_names: list) -> str:
    """多分支节点的一行分支定义"""
    op_opts = "".join(
        f'<option value="{o}"{" selected" if o == op else ""}>{l}</option>'
        for o, l in CONDITION_OPS
    )
    next_opts = "".join(
        f'<option value="{esc(nn)}"{(" selected" if nn == next_node else "")}>{esc(nn)}</option>'
        for nn in node_names
    )
    return (
        "<tr>"
        f'<td><input type="text" name="branch_label[]" value="{esc(str(label))}" placeholder="如 大于1"></td>'
        f'<td><select name="branch_op[]">{op_opts}</select></td>'
        f'<td><input type="text" name="branch_value[]" value="{esc(str(value))}" placeholder="比较值"></td>'
        f'<td><select name="branch_next[]">'
        f'<option value="">（未指定 → 中止）</option>'
        f'<option value="{END_TARGET}"{" selected" if next_node == END_TARGET else ""}>（到此结束）</option>'
        f'{next_opts}</select></td>'
        "<td><button type='button' class='btn btn-ghost btn-sm' onclick=\"this.closest('tr').remove()\">删除</button></td>"
        "</tr>"
    )


def _switch_editor(p: dict, node_names: list) -> str:
    """多分支判断节点的参数区：分支表格 + 兜底跳转"""
    branches = p.get("branches") or []
    if not isinstance(branches, list):
        branches = []
    rows = ""
    for b in branches:
        if not isinstance(b, dict):
            continue
        rows += _switch_row(
            b.get("label", ""),
            b.get("op", "=="),
            b.get("value", ""),
            b.get("next", ""),
            node_names,
        )
    if not rows:
        rows = _switch_row("", "==", "", "", node_names)
    return f"""
    <div style="flex:1;min-width:100%">
        <label class="param-label">分支定义（从上到下匹配，命中第一个即跳转）</label>
        <div class="table-wrap">
        <table id="switchTbl" style="margin-top:6px">
            <thead><tr><th style="width:150px">分支名 *</th><th style="width:150px">条件</th><th style="width:150px">值</th><th style="width:200px">跳转到节点</th><th></th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
        </div>
        <button type="button" class="btn btn-ghost btn-sm" onclick="addSwitchRow(this)" style="margin-top:8px">+ 添加分支</button>
    </div>
    <div style="flex:1;min-width:100%;margin-top:10px">
        <label class="param-label">兜底跳转（所有分支都不命中时）</label>
        {_jump_select("params[default_next]", str(p.get("default_next") or ""), node_names, "（中止）", allow_end=True)}
    </div>
    """


def _output_refs_hint(node_info: list) -> str:
    """输出节点参数区的可引用变量提示：列出已保存节点的输出字段与引用示例"""
    if not node_info:
        return ""
    lines = []
    for name, ntype in node_info:
        fields = NODE_OUTPUTS.get(ntype)
        if not fields:
            continue
        first = fields.split(",")[0].split("(")[0].strip()
        example = f"{{{{{name}.{first}}}}}" if first else ""
        lines.append(
            f'<div style="margin:2px 0"><code style="color:#0369a1">{{{esc(name)}}}</code> '
            f'<span style="color:#94a3b8">→</span> {esc(fields)}　'
            f'<span style="color:#0d9488">例：<code>{example}</code></span></div>'
        )
    return (
        '<div class="param-note note-dark">'
        "<b>📎 可引用前序节点输出</b>（在文本模板中写 <code>{{{{节点名.字段}}}}</code> 即可引用）："
        + "".join(lines)
        + "</div>"
    )


def _table_node_hint(nt: str) -> str:
    """表格类节点（创建/查询/插入/更新/删除）的详细使用说明"""
    title = {
        "table_query": "查询表格",
        "table_insert": "插入表格",
        "table_update": "更新表格",
        "table_delete": "删除表格",
        "table_create": "创建表格",
    }.get(nt, "表格")
    perm = "「读取」权限" if nt == "table_query" else "「读写」权限"

    if nt == "table_create":
        detail = (
            '<div style="margin:3px 0"><b>① 表格名称</b>：新表格的名称，需唯一（重复会报错）。</div>'
            '<div style="margin:3px 0"><b>② 字段定义（下方表单）</b>：每行定义一个字段：'
            "字段名（字母开头，仅字母/数字/下划线）、类型（varchar/int/float/datetime/text/json）、"
            "中文名（可选）、是否必填。点「+ 添加字段」可增加字段行。</div>"
            '<div style="margin:3px 0"><b>③ 自动授权</b>：创建成功后自动授权给工作流执行身份的智能体'
            "（读写权限），后续节点可直接插入/查询该表。</div>"
            '<div style="margin:3px 0"><b>④ 输出字段</b>：<code>table_id</code>（表格 id）、'
            "<code>table_key</code>（表格标识，如 tbl_12），后续节点可用它作为 table 参数。</div>"
        )
    elif nt == "table_query":
        detail = (
            '<div style="margin:3px 0"><b>① 查询条件（下方表单）</b>：每行一个条件'
            "（字段名 + 操作符 + 值），多行之间按 AND 连接；不填任何条件 = 查询全部行。</div>"
            '<div style="margin:2px 0;padding-left:14px">操作符支持 <code>=&nbsp;&nbsp;!=&nbsp;&nbsp;&gt;&nbsp;&nbsp;&lt;&nbsp;&nbsp;&gt;=&nbsp;&nbsp;&lt;=&nbsp;&nbsp;like</code>，'
            "like 用于模糊匹配（值里写 <code>%关键词%</code>）。</div>"
            '<div style="margin:2px 0;padding-left:14px">值可引用变量，如比较值填 <code>{{input.user_text}}</code>。</div>'
            '<div style="margin:3px 0"><b>② limit</b>：最多返回多少行，默认 50，最大 200。</div>'
            '<div style="margin:3px 0"><b>③ 输出字段</b>：<code>rows</code>（行列表）+ <code>count</code>（命中行数）。'
            "每行是一个对象，含 <code>id</code> 及该表全部字段。</div>"
            '<div style="margin:2px 0"><b>引用示例</b>：第一行的「订单号」→ <code>{{节点名.rows.0.order_no}}</code>；'
            "第一行的 id → <code>{{节点名.rows.0.id}}</code>；命中行数 → <code>{{节点名.count}}</code>。</div>"
            '<div style="margin:2px 0">查询结果可继续交给「条件/多分支」判断，或用「代码节点」遍历 rows 逐行处理。</div>'
        )
    elif nt == "table_insert":
        detail = (
            '<div style="margin:3px 0"><b>插入数据（下方表单）</b>：每行填「字段名 → 值」，'
            "字段名为该表格的字段，值为要写入的内容；空字段名或空值的行会被忽略。</div>"
            '<div style="margin:2px 0;padding-left:14px">例：字段名填 <code>order_no</code>，'
            '值填 <code>SO1</code> → 写入 order_no=SO1。</div>'
            '<div style="margin:2px 0;padding-left:14px">值可引用变量：<code>{{input.user_text}}</code>；'
            "数字/布尔值可自动识别（如 100 → 数字）。</div>"
            '<div style="margin:3px 0"><b>输出字段</b>：<code>id</code>（新插入行的自增 id），'
            "后续可引用 <code>{{节点名.id}}</code>。</div>"
        )
    elif nt == "table_update":
        detail = (
            '<div style="margin:3px 0"><b>行 id *</b>：要更新的行 id。'
            "常与「查询表格」配合：<code>{{查询节点名.rows.0.id}}</code>。</div>"
            '<div style="margin:3px 0"><b>更新字段（下方表单）</b>：每行填「字段名 → 新值」，'
            "只写需要更新的字段；空字段名或空值的行会被忽略。</div>"
            '<div style="margin:2px 0;padding-left:14px">例：字段名填 <code>status</code>，值填 <code>已发货</code> → 更新该字段。</div>'
            '<div style="margin:2px 0;padding-left:14px">值可引用变量：<code>{{input.user_text}}</code>；'
            "数字/布尔值可自动识别。</div>"
            '<div style="margin:3px 0"><b>输出字段</b>：<code>ok</code>（是否更新成功），'
            "后续可引用 <code>{{节点名.ok}}</code>。</div>"
        )
    else:  # table_delete
        detail = (
            '<div style="margin:3px 0"><b>行 id *</b>：要删除的行 id。'
            "常与「查询表格」配合：<code>{{查询节点名.rows.0.id}}</code>。</div>"
            '<div style="margin:3px 0"><b>输出字段</b>：<code>ok</code>（是否删除成功），'
            "后续可引用 <code>{{节点名.ok}}</code>。</div>"
        )

    if nt == "table_create":
        pre = (
            '<div style="margin:2px 0">• 无需事先授权：创建成功后自动授权给工作流执行身份的智能体'
            "（读写权限），可直接用「插入表格 / 查询表格」节点操作新表。</div>"
        )
    else:
        pre = (
            '<div style="margin:2px 0">• 前置条件：工作流需绑定 <b>智能体</b>，且该智能体已被 <b>授权</b> 此表格'
            f"（表格管理 → 授权，需 {perm}），否则节点会报错。</div>"
        )
    return (
        '<div class="param-note note-blue">'
        f"<b>📖 {title} 使用说明</b>"
        + pre
        + '<div style="margin:2px 0">• 所有参数均支持 <code>{{变量}}</code> 模板，'
        "如 <code>{{input.user_text}}</code>、<code>{{前序节点名.字段}}</code>。</div>"
        + detail
        + "</div>"
    )


def _http_node_hint() -> str:
    """联网请求节点（get/post）的输出字段说明"""
    return (
        '<div class="param-note note-blue">'
        "<b>📖 联网请求 输出说明</b>"
        '<div style="margin:2px 0">• <b>status_code</b>：HTTP 状态码（数字）。'
        "200=请求成功，404=资源或接口不存在，500=服务器内部错误等；非 2xx 时 text 仍会返回响应内容，"
        "可用「条件/多分支」节点判断 status_code 走不同分支。</div>"
        '<div style="margin:2px 0">• <b>text</b>：服务器返回的<b>响应体原文</b>（字符串），即接口实际返回的内容，'
        "可能是 JSON、HTML 或纯文本。用 <code>{{节点名.text}}</code> 即可引用整段原文。</div>"
        '<div style="margin:2px 0">• 注意：text 是<b>字符串</b>。若接口返回的是 JSON，'
        "需在「代码」节点先解析再取字段，示例：</div>"
        "<pre>raw = ctx['n2'].get('text', '{}')\n"
        "data = json.loads(raw)\n"
        "output['订单状态'] = data.get('status', '')</pre>"
        '<div style="margin:2px 0">• url / data / headers 均支持 <code>{{变量}}</code> 模板，'
        "如 <code>{{input.user_text}}</code>、<code>{{前序节点名.字段}}</code>；"
        "data / headers 在下方表单中按「字段名/键 → 值」逐行填写。</div>"
        "</div>"
    )


def _device_node_hint() -> str:
    """设备指令节点「等待结果」选项的含义说明"""
    rows = [
        (
            "等待执行回执（默认）",
            "只等设备回执「指令执行了没」（成功/失败列表），不返回业务数据，适用所有指令",
        ),
        (
            "501 群详情",
            "等设备上报指定群详情；需在 params 传 <code>selectList</code>（群名）→ 返回群主、成员数、成员列表、公告",
        ),
        (
            "502 好友资料",
            "等设备上报指定好友资料；需在 params 传 <code>friend</code> → 返回姓名、手机号、备注、企业",
        ),
        ("503 我的信息", "等设备上报当前登录账号信息 → 返回名字、ID、企业、部门"),
        ("505 最近聊天", "等设备上报最近会话列表 → 返回各会话标题、最后一条消息"),
        ("506 企业列表", "等设备上报企业列表"),
        ("507 全部好友", "等设备上报通讯录全部好友 → 返回 items 好友列表"),
        ("508 全部群", "等设备上报全部群列表 → 返回 items 群列表"),
    ]
    trs = "".join(
        '<tr style="border-top:1px solid #e2e8f0"><td style="padding:3px 6px;white-space:nowrap">'
        f'{opt}</td><td style="padding:3px 6px;line-height:1.6">{desc}</td></tr>'
        for opt, desc in rows
    )
    return (
        '<div class="param-note note-blue">'
        "<b>📖 等待结果 选项说明</b>"
        '<div style="margin:2px 0">设备指令是<b>异步</b>的：指令下发后，设备执行完才把数据通过 WebSocket 上报回来。'
        "「等待结果」决定本节点等待并捕获哪一类上报数据；不选则只等执行回执。</div>"
        '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:4px">'
        f"<tbody>{trs}</tbody></table>"
        '<div style="margin:3px 0">提示：发 507/508 这类「获取列表」指令时，配合对应等待类型，'
        "<code>{{节点名.result.items}}</code> 即为列表；不确定的指令选默认「等待执行回执」即可。</div>"
        "</div>"
    )


def _cmd_param_script() -> str:
    """设备指令节点参数表单脚本：随 cmd_type 切换显示对应字段组与参数说明"""
    payload = json.dumps(CMD_PARAM_INFO, ensure_ascii=False).replace("</", "<\\/")
    return (
        "<script>"
        "(function(){"
        f"var INFO={payload};"
        "var selects=document.querySelectorAll('select[name=\"params[cmd_type]\"]');"
        "selects.forEach(function(sel){"
        "if(sel.__cmdParamBinded)return;sel.__cmdParamBinded=1;"
        "var form=sel.closest('form');if(!form)return;"
        "var hint=form.querySelector('.cmd-param-hint');"
        "function upd(){"
        "var v=sel.value;"
        "/* 父级节点类型块是否可见：隐藏时必须让整块控件保持禁用，否则其中 required 输入"
        "会触发浏览器表单校验（invalid form control is not focusable）导致表单无法提交 */"
        "var nv=sel.closest('.nparams');"
        "var parentVisible=!nv||nv.style.display!=='none';"
        "form.querySelectorAll('.cparams').forEach(function(d){"
        "var show=(d.getAttribute('data-cmdtype')===v);"
        "d.style.display=show?'block':'none';"
        "d.querySelectorAll('input,select,textarea').forEach(function(c){c.disabled=(!show||!parentVisible);});"
        "});"
        "/* 同指令号多用法（如 213）切换：按当前用法显示字段组 */"
        "var usageSel=form.querySelector('select[name=\"params[usage]\"]');"
        "if(usageSel){"
        "var uv=usageSel.value||'';"
        "form.querySelectorAll('.cmd-usage-fields').forEach(function(g){"
        "var show=(g.getAttribute('data-cmd')===v)&&(g.getAttribute('data-usage')===uv);"
        "g.style.display=show?'flex':'none';"
        "g.querySelectorAll('input,select,textarea').forEach(function(c){c.disabled=(!show||!parentVisible);});"
        "});"
        "}"
        "if(hint){"
        "var it=INFO[v];"
        "if(it){"
        "hint.innerHTML='<b>📋 指令 '+v+' 参数说明</b>：'+it.desc"
        "+'<div style=\"margin:2px 0 0 0\">JSON 示例：<code style=\"font-family:Consolas,monospace\">'+it.example+'</code></div>'"
        "+'<div style=\"margin:2px 0 0 0;color:#92400e\">在下方表单填写即可；字段值支持 {{变量}} 模板，列表字段多个值用逗号分隔。</div>';"
        "}else{"
        "hint.innerHTML='选择指令类型后，此处显示该指令的参数说明。';"
        "}"
        "}"
        "}"
        "sel.__cmdParamUpd=upd;sel.addEventListener('change',upd);"
        "/* 用法下拉切换时刷新字段组显隐 */"
        "form.querySelectorAll('select[name=\"params[usage]\"]').forEach(function(us){"
        "if(us.__usageBinded)return;us.__usageBinded=1;"
        "us.addEventListener('change',upd);"
        "});"
        "upd();"
        "});"
        "})();"
        "</script>"
    )


# 创建任务节点表单脚本：按 trigger_type / action_type 切换显示对应配置字段组。
# 隐藏组的控件置为 disabled（不随表单提交），避免浏览器 required 校验拦截提交。
_TASK_CREATE_SCRIPT = """<script>
(function(){
    var selects=document.querySelectorAll('select[name="params[trigger_type]"],select[name="params[action_type]"]');
    selects.forEach(function(sel){
        if(sel.__taskCreateBinded)return;sel.__taskCreateBinded=1;
        var form=sel.closest('form');if(!form)return;
        function upd(){
            var tt=form.querySelector('select[name="params[trigger_type]"]');
            var at=form.querySelector('select[name="params[action_type]"]');
            var fs=form.querySelector('select[name="params[trigger_freq]"]');
            var ms=form.querySelector('select[name="params[trigger_mode]"]');
            var rms=form.querySelector('select[name="params[repeat_method]"]');
            var cds=form.querySelector('select[name="params[cron_def]"]');
            var tv=tt?tt.value:'schedule';
            var av=at?at.value:'workflow';
            var fv=fs?fs.value:'daily';
            var mv=ms?ms.value:'repeat';
            var rmv=rms?rms.value:'cron';
            var cdv=cds?cds.value:'selector';
            function setGroup(cls,show){
                form.querySelectorAll('.'+cls).forEach(function(g){
                    g.style.display=show?'flex':'none';
                    g.querySelectorAll('input,select,textarea').forEach(function(c){c.disabled=!show;});
                });
            }
            setGroup('task-trigger-schedule',tv==='schedule');
            setGroup('task-trigger-condition',tv==='condition');
            setGroup('task-action-workflow',av==='workflow');
            setGroup('task-action-agent',av==='agent');
            setGroup('task-mode-repeat',tv==='schedule'&&mv==='repeat');
            setGroup('task-mode-once',tv==='schedule'&&mv==='once');
            setGroup('task-rm-cron',tv==='schedule'&&mv==='repeat'&&rmv==='cron');
            setGroup('task-rm-interval',tv==='schedule'&&mv==='repeat'&&rmv==='interval');
            setGroup('task-cd-selector',tv==='schedule'&&mv==='repeat'&&rmv==='cron'&&cdv==='selector');
            setGroup('task-cd-format',tv==='schedule'&&mv==='repeat'&&rmv==='cron'&&cdv==='format');
            setGroup('task-freq-weekdays',tv==='schedule'&&mv==='repeat'&&rmv==='cron'&&cdv==='selector'&&fv==='weekly');
            setGroup('task-freq-monthly',tv==='schedule'&&mv==='repeat'&&rmv==='cron'&&cdv==='selector'&&fv==='monthly');
        }
        form.querySelectorAll('select[name="params[trigger_type]"],select[name="params[action_type]"],select[name="params[trigger_freq]"],select[name="params[trigger_mode]"],select[name="params[repeat_method]"],select[name="params[cron_def]"]')
            .forEach(function(s){if(s!==sel)s.addEventListener('change',upd);});
        // 工作流参数行：添加/删除（最少保留一行）
        var wfBox=form.querySelector('.wf-param-box');
        if(wfBox){
            var wfAdd=form.querySelector('.wf-param-add');
            if(wfAdd)wfAdd.addEventListener('click',function(){
                var proto=wfBox.querySelector('.wf-param-row');
                var n=proto.cloneNode(true);
                n.querySelectorAll('input').forEach(function(i){i.value='';});
                wfBox.appendChild(n);
            });
            wfBox.addEventListener('click',function(e){
                if(e.target.classList.contains('wf-param-del')){
                    var rows=wfBox.querySelectorAll('.wf-param-row');
                    if(rows.length>1)e.target.closest('.wf-param-row').remove();
                }
            });
        }
        upd();
    });
})();
</script>"""


def _cmd_param_fields(raw) -> str:
    """设备指令节点参数表单：按指令类型分组渲染 params[p_xxx] 输入，取代手填 JSON。

    已保存参数（raw 为 dict 或 JSON 字符串）会回填到对应字段；空字段保存时自动忽略。
    """
    extra = raw
    if isinstance(extra, str):
        try:
            extra = json.loads(extra) if extra.strip() else {}
        except Exception:
            extra = {}
    if not isinstance(extra, dict):
        extra = {}

    def _render_cmd_field(f: dict) -> str:
        """渲染一个指令参数字段（params[p_xxx]）"""
        fname = f["name"]
        required = bool(f.get("required"))
        val = extra.get(fname, "")
        if isinstance(val, (dict, list)):
            val = json.dumps(val, ensure_ascii=False)
        label = f["label"] + (" *" if required else "")
        hints = []
        if f.get("list"):
            hints.append("多个值用逗号分隔")
        if not required:
            hints.append("可选")
        return _field(
            f"p_{fname}",
            label,
            val,
            f.get("ph") or "",
            f.get("kind") or "text",
            required,
            hint="；".join(hints),
            rows=int(f.get("rows") or 3),
        )

    blocks = []
    for cmd, _label in CMD_OPTIONS:
        info = CMD_PARAM_INFO.get(cmd) or {}
        fields = info.get("fields") or []
        usages = info.get("usages") or []
        if usages:
            # 同一指令号拆成多种用法（如 213 修改好友备注 / 按手机号添加好友）：
            # 顶部「用法」下拉切换，仅显示当前用法的字段
            cur_usage = "add" if extra.get("friend_phone") else str(usages[0]["value"])
            usage_opts = "".join(
                f'<option value="{u["value"]}"{" selected" if u["value"] == cur_usage else ""}>{u["label"]}</option>'
                for u in usages
            )
            inner = [
                '<div style="flex:1;min-width:100%">'
                '<label class="param-label">用法</label>'
                f'<select name="params[usage]" class="cmd-usage-sel" style="min-width:220px">{usage_opts}</select>'
                "</div>"
            ]
            for u in usages:
                usage = u["value"]
                flds = "".join(_render_cmd_field(f) for f in (u.get("fields") or []))
                inner.append(
                    f'<div class="cmd-usage-fields" data-cmd="{cmd}" data-usage="{usage}" '
                    f'style="display:{"" if usage == cur_usage else "none"};flex:1;min-width:100%;gap:10px">{flds}</div>'
                )
            inner_html = "".join(inner)
        elif fields:
            inner_html = "".join(_render_cmd_field(f) for f in fields)
        else:
            inner_html = (
                '<div class="muted" style="font-size:12px;padding:6px 2px">'
                "该指令无需参数，直接下发即可。</div>"
            )
        blocks.append(
            '<div class="cparams" data-cmdtype="%s" style="display:none;flex:1;min-width:100%%">'
            '<div style="display:flex;flex-wrap:wrap;gap:10px">%s</div></div>'
            % (cmd, inner_html)
        )
    return "".join(blocks)


def _table_fields_editor(p: dict) -> str:
    """创建表格节点的字段定义编辑器（表单行：字段名/类型/中文名/必填，替代 JSON 输入）"""
    _TF_TYPES = ("varchar", "int", "float", "datetime", "text", "json")

    def _row(name="", ftype="varchar", label="", required=False) -> str:
        type_opts = "".join(
            f'<option value="{t}"{" selected" if t == ftype else ""}>{t}</option>'
            for t in _TF_TYPES
        )
        return (
            '<div class="tf-row" style="display:flex;gap:8px;align-items:center;margin-bottom:6px">'
            f'<input type="text" name="tf_name[]" value="{esc(name)}" placeholder="字段名(字母开头)" '
            'style="flex:1;min-width:90px" required>'
            f'<select name="tf_type[]" style="width:110px">{type_opts}</select>'
            f'<input type="text" name="tf_label[]" value="{esc(label)}" placeholder="中文名(可选)" '
            'style="flex:1;min-width:90px">'
            f'<select name="tf_required[]" style="width:64px">'
            f'<option value="0"{" selected" if not required else ""}>否</option>'
            f'<option value="1"{" selected" if required else ""}>是</option>'
            "</select>"
            "<button type='button' class='btn btn-ghost btn-sm' "
            "onclick=\"this.closest('.tf-row').remove()\">删除</button>"
            "</div>"
        )

    fields = p.get("fields")
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except Exception:
            fields = None
    rows = ""
    if isinstance(fields, list):
        for f in fields:
            if not isinstance(f, dict):
                continue
            req = f.get("required")
            if isinstance(req, str):
                req = req.lower() in ("1", "true", "yes", "on")
            rows += _row(
                str(f.get("name") or ""),
                str(f.get("type") or "varchar"),
                str(f.get("label") or ""),
                bool(req),
            )
    if not rows:
        rows = _row()
    return f"""
    <div style="flex:1;min-width:100%">
        <label class="param-label">字段定义（每行一个字段，至少 1 个）</label>
        <div class="tf-body">{rows}</div>
        <button type="button" class="btn btn-ghost btn-sm" onclick="addFieldRow(this)" style="margin-top:2px">+ 添加字段</button>
    </div>"""


def _kv_rows_from(p, param_key, kind="kv"):
    """把保存的参数值归一化为 [(field, op, value), ...]，用于回填键/值行表单。
    兼容 dict（{字段:值}）、cond 列表（[{field,op,value}]）与 JSON 字符串（旧格式）。
    """
    raw = p.get(param_key) if isinstance(p, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    rows = []
    if kind == "cond":
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                rows.append(
                    (
                        str(item.get("field") or ""),
                        str(item.get("op") or "="),
                        "" if item.get("value") is None else str(item.get("value")),
                    )
                )
        elif isinstance(raw, dict):
            for f, v in raw.items():
                rows.append((str(f), "=", "" if v is None else str(v)))
    else:
        if isinstance(raw, dict):
            for f, v in raw.items():
                rows.append((str(f), "", "" if v is None else str(v)))
    if not rows:
        rows = [("", "", "")]
    return rows


def _kv_row_html(prefix, kind, field="", op="=", value=""):
    """键/值 或 条件 行表单的一行 HTML"""
    del_btn = (
        "<button type='button' class='btn btn-ghost btn-sm' "
        "onclick=\"this.closest('.kv-row').remove()\">删除</button>"
    )
    ph = "字段名" if kind == "cond" else "字段名/键"
    fld = (
        f'<input type="text" name="{prefix}_field[]" value="{esc(field)}" '
        f'placeholder="{ph}" style="flex:1;min-width:90px" required>'
    )
    if kind == "cond":
        op_opts = "".join(
            f'<option value="{o}"{" selected" if o == op else ""}>{o}</option>'
            for o in ("=", "!=", ">", "<", ">=", "<=", "like")
        )
        val = (
            f'<select name="{prefix}_op[]" style="width:80px">{op_opts}</select>'
            f'<input type="text" name="{prefix}_value[]" value="{esc(value)}" '
            'placeholder="比较值" style="flex:1;min-width:90px">'
        )
    else:
        val = (
            f'<input type="text" name="{prefix}_value[]" value="{esc(value)}" '
            'placeholder="值" style="flex:1;min-width:90px">'
        )
    return (
        '<div class="kv-row" style="display:flex;gap:8px;align-items:center;margin-bottom:6px">'
        f"{fld}{val}{del_btn}</div>"
    )


def _kv_editor(prefix, label, p, param_key, kind="kv"):
    """通用 键/值 或 条件 行编辑器（替代 JSON 输入）。
    prefix: 表单数组名前缀（cq/ir/uf/hg/hpd/hph）
    kind: kv=两列(字段名,值)；cond=三列(字段名,操作符,值)
    保存的数据从 p[param_key] 回填。
    """
    rows_html = "".join(_kv_row_html(prefix, kind, f, op, v) for f, op, v in _kv_rows_from(p, param_key, kind))
    return (
        '<div class="kv-editor" style="flex:1;min-width:100%">'
        f'<label class="param-label">{label}</label>'
        f'<div class="kv-body">{rows_html}</div>'
        f'<button type="button" class="btn btn-ghost btn-sm" data-kind="{kind}" data-pf="{prefix}" '
        'onclick="addKVRow(this)" style="margin-top:2px">+ 添加</button>'
        "</div>"
    )


def _params_fields(
    node_type: str,
    p: dict,
    tables: list,
    node_names: list = None,
    node_info: list = None,
    agents: list = None,
    skills: list = None,
) -> str:
    """按节点类型渲染参数输入区（div 带 data-ptype 属性，JS 按类型切换）"""
    node_names = node_names or []
    table_opts = [(t.table_key, f"{t.name} ({t.table_key})") for t in tables]
    blocks = []
    for nt in NODE_TYPE_NAMES:
        parts = []
        if nt == "llm":
            # 选填：指定此节点使用的 LLM（默认=调用此工作流的智能体）
            if agents:
                agent_opts = "".join(
                    f'<option value="{a.id}"'
                    f'{" selected" if str(p.get("agent_id") or "") == str(a.id) else ""}>'
                    f"{esc(a.name)}</option>"
                    for a in agents
                )
                parts.append(
                    f'<div style="flex:1;min-width:180px">'
                    f'<label class="param-label">使用 LLM（选填）</label>'
                    f'<select name="params[agent_id]">'
                    f'<option value="">（默认：调用此工作流的智能体）</option>'
                    f"{agent_opts}</select>"
                    f'<div class="form-hint">指定后此节点使用所选智能体的模型/系统提示词/知识库（如安全审查 agent）</div>'
                    f"</div>"
                )
            # 选填：挂载技能（注入行为指导，软；如审查节点挂「安全审查」技能）
            if skills:
                raw_sids = p.get("skill_ids") or []
                if isinstance(raw_sids, str):
                    try:
                        raw_sids = json.loads(raw_sids) if raw_sids.strip().startswith("[") else []
                    except Exception:
                        raw_sids = []
                sel_sids = {int(x) for x in raw_sids if str(x).isdigit()}
                skill_chips = "".join(
                    f'<label class="chip-check">'
                    f'<input type="checkbox" name="params[skill_ids][]" value="{s["id"]}"'
                    f'{" checked" if s["id"] in sel_sids else ""}> {esc(s["name"])}</label>'
                    for s in skills
                )
                parts.append(
                    f'<div style="flex:1;min-width:100%">'
                    f'<label class="param-label">挂载技能（选填）</label>'
                    f'<div class="inline-flex" style="gap:8px;flex-wrap:wrap">{skill_chips}</div>'
                    f'<div class="form-hint">节点显式挂载 = 该节点临时生效，不受智能体勾选约束（如审查节点挂「安全审查基线」）；软指导，不承载安全强制</div>'
                    f"</div>"
                )
            parts.append(
                _field(
                    "prompt",
                    "提示词模板 *",
                    p.get("prompt"),
                    "结合上下文与业务规则，可用 {{变量}} 引用前序节点结果",
                    "textarea",
                    True,
                    hint="追加而非覆盖：本提示词作为「用户消息」追加在智能体系统提示词之后，不会替换智能体人设；所选智能体的知识库检索内容与挂载技能会先追加进系统提示词",
                )
            )
        elif nt == "table_query":
            parts.append(
                _field(
                    "table",
                    "表格 *",
                    p.get("table"),
                    "",
                    "select",
                    True,
                    options=table_opts,
                )
            )
            parts.append(_kv_editor("cq", "查询条件（每行一个条件，多行按 AND 连接）", p, "where", kind="cond"))
            parts.append(
                _field(
                    "limit",
                    "返回行数 limit",
                    p.get("limit", 50),
                    "默认 50，最大 200",
                    "number",
                )
            )
            parts.append(_table_node_hint(nt))
        elif nt == "table_create":
            parts.append(
                _field(
                    "name",
                    "表格名称 *",
                    p.get("name"),
                    "如：客户名单（需唯一）",
                    required=True,
                )
            )
            parts.append(
                _field("description", "描述", p.get("description"), "可选，表格用途说明")
            )
            parts.append(_table_fields_editor(p))
            parts.append(_table_node_hint(nt))
        elif nt in ("table_insert", "table_update", "table_delete"):
            parts.append(
                _field(
                    "table",
                    "表格 *",
                    p.get("table"),
                    "",
                    "select",
                    True,
                    options=table_opts,
                )
            )
            if nt == "table_insert":
                parts.append(_kv_editor("ir", "插入数据（每行：字段名 → 值）", p, "row"))
            else:
                parts.append(
                    _field(
                        "id", "行 id *", p.get("id"), "要操作的行 id", "number", True
                    )
                )
                if nt == "table_update":
                    parts.append(_kv_editor("uf", "更新字段（每行：字段名 → 新值）", p, "fields"))
            parts.append(_table_node_hint(nt))
        elif nt == "http_get":
            parts.append(
                _field(
                    "url",
                    "URL *",
                    p.get("url"),
                    "https://api.example.com/query",
                    required=True,
                    span=2,
                )
            )
            parts.append(_kv_editor("hg", "请求头 headers（可选，每行：键 → 值）", p, "headers"))
            parts.append(_http_node_hint())
        elif nt == "http_post":
            parts.append(
                _field(
                    "url",
                    "URL *",
                    p.get("url"),
                    "https://api.example.com/query",
                    required=True,
                    span=2,
                )
            )
            parts.append(_kv_editor("hpd", "提交数据 data（每行：字段名 → 值）", p, "data"))
            parts.append(_kv_editor("hph", "请求头 headers（可选，每行：键 → 值）", p, "headers"))
            parts.append(_http_node_hint())
        elif nt == "device_command":
            parts.append(
                _field(
                    "cmd_type",
                    "指令类型 *",
                    str(p.get("cmd_type", "")),
                    "",
                    "select",
                    True,
                    options=CMD_OPTIONS,
                )
            )
            parts.append(
                _cmd_param_fields(p.get("params"))
            )
            parts.append(
                '<div class="cmd-param-hint param-hint-bar">'
                "选择指令类型后，此处显示该指令的参数说明（列表字段支持逗号分隔）。</div>"
            )
            parts.append(
                _field(
                    "expect_type",
                    "等待结果",
                    str(p.get("expect_type", "")),
                    "",
                    "select",
                    options=EXPECT_TYPE_OPTIONS,
                )
            )
            parts.append(
                _field("wait_sec", "等待秒数", p.get("wait_sec", 8), "默认 8", "number")
            )
            parts.append(_device_node_hint())
            parts.append(_cmd_param_script())
        elif nt == "task_create":
            # 回填：兼容旧节点存的 trigger_spec/action_spec（JSON）与新节点的扁平字段
            ts = p.get("trigger_spec")
            if isinstance(ts, str):
                try:
                    ts = json.loads(ts)
                except Exception:
                    ts = {}
            if not isinstance(ts, dict):
                ts = {}
            aspec = p.get("action_spec")
            if isinstance(aspec, str):
                try:
                    aspec = json.loads(aspec)
                except Exception:
                    aspec = {}
            if not isinstance(aspec, dict):
                aspec = {}
            parts.append(
                _field("name", "任务名 *", p.get("name"), "新任务的名字，可用 {{变量}}", required=True, span=2)
            )
            parts.append(
                _field("trigger_type", "触发类型 *", str(p.get("trigger_type", "schedule")), "", "select", True,
                       options=[("schedule", "定时触发"), ("condition", "条件触发")])
            )
            # 触发配置：定时（schedule）—— 重复（时间组件 + 次数）/ 单次（绝对或相对当前时间）
            freq_data = cron_to_form(ts.get("cron")) if ts.get("cron") else None
            t_time = (freq_data or {}).get("time") or p.get("trigger_time", "09:00")
            t_freq = (freq_data or {}).get("freq") or p.get("trigger_freq", "daily")
            t_weekdays = (freq_data or {}).get("weekdays") or p.get("trigger_weekdays", []) or []
            t_monthday = (freq_data or {}).get("monthday") or p.get("trigger_monthday", 1)
            adv_cron = "" if freq_data else (ts.get("cron") or p.get("trigger_cron", ""))
            t_mode = p.get("trigger_mode") or ("once" if ts.get("once_at") is not None else "repeat")
            once_ts_raw = ts.get("once_at")
            if once_ts_raw is not None:
                try:
                    once_val = datetime.fromtimestamp(int(once_ts_raw)).strftime("%Y-%m-%dT%H:%M")
                except Exception:
                    once_val = ""
            else:
                once_val = p.get("trigger_once_at", "")
            rep_cnt = ts.get("repeat_count") or p.get("trigger_repeat_count", 0)
            off_min = (int(ts.get("offset_sec") or 0) // 60) if ts.get("offset_sec") else (p.get("trigger_offset_minutes", 0))
            once_off = p.get("trigger_once_offset_minutes", "")
            # 重复方式回填：有 cron → 定时计划；只有 interval_sec → 固定间隔（与调度优先级一致）
            t_method = "interval" if (ts.get("interval_sec") is not None and not ts.get("cron")) else "cron"
            t_cron_def = "selector" if freq_data else "format"
            rm_cron_style = "flex" if t_method == "cron" else "none"
            rm_int_style = "flex" if t_method == "interval" else "none"
            cd_sel_style = "flex" if t_cron_def == "selector" else "none"
            cd_fmt_style = "flex" if t_cron_def == "format" else "none"
            mode_opts = "".join(
                f'<option value="{m}"{" selected" if t_mode == m else ""}>{label}</option>'
                for m, label in (("repeat", "重复执行"), ("once", "单次执行"))
            )
            rep_style = "flex" if t_mode == "repeat" else "none"
            once_style = "flex" if t_mode == "once" else "none"
            freq_opts = "".join(
                f'<option value="{f}"{" selected" if t_freq == f else ""}>{label}</option>'
                for f, label in (("daily", "每天"), ("weekly", "每周"), ("monthly", "每月"))
            )
            wd_style = "flex" if t_freq == "weekly" else "none"
            md_style = "flex" if t_freq == "monthly" else "none"
            wd_checks = "".join(
                f'<label style="display:inline-flex;align-items:center;gap:2px;margin:0 6px 4px 0">'
                f'<input type="checkbox" name="params[trigger_weekdays][]" value="{i}"'
                f'{" checked" if str(i) in [str(x) for x in t_weekdays] else ""}>'
                f'周{_WEEKDAY_LABELS[i]}</label>'
                for i in range(7)
            )
            parts.append(
                '<div class="task-trigger-schedule" style="display:flex;flex-wrap:wrap;gap:10px">'
                + '<div style="flex:1;min-width:110px"><label class="param-label">触发方式</label>'
                  f'<select name="params[trigger_mode]" class="task-mode-select">{mode_opts}</select></div>'
                # 重复模式：先分重复方式（定时计划 cron / 固定间隔），cron 再分定义方式（选择器 / 表达式），最后公共重复次数
                + f'<div class="task-mode-repeat" style="display:{rep_style};flex-wrap:wrap;gap:10px;width:100%">'
                + '<div style="flex:1;min-width:150px"><label class="param-label">重复方式</label>'
                  f'<select name="params[repeat_method]" class="task-rm-select">'
                  f'<option value="cron"{" selected" if t_method == "cron" else ""}>定时计划（cron）</option>'
                  f'<option value="interval"{" selected" if t_method == "interval" else ""}>固定间隔</option>'
                  "</select></div>"
                # 定时计划（cron）：定义方式（选择器 / cron 表达式）
                + f'<div class="task-rm-cron" style="display:{rm_cron_style};flex-wrap:wrap;gap:10px;width:100%">'
                + '<div style="flex:1;min-width:150px"><label class="param-label">定义方式</label>'
                  f'<select name="params[cron_def]" class="task-cd-select">'
                  f'<option value="selector"{" selected" if t_cron_def == "selector" else ""}>选择器（执行时间+频率）</option>'
                  f'<option value="format"{" selected" if t_cron_def == "format" else ""}>cron 表达式</option>'
                  "</select></div>"
                + f'<div class="task-cd-selector" style="display:{cd_sel_style};flex-wrap:wrap;gap:10px;width:100%">'
                + '<div style="flex:1;min-width:150px"><label class="param-label">执行时间 *</label>'
                  f'<input type="time" name="params[trigger_time]" value="{esc(t_time)}" required></div>'
                + '<div style="flex:1;min-width:110px"><label class="param-label">重复频率</label>'
                  f'<select name="params[trigger_freq]" class="task-freq-select">{freq_opts}</select></div>'
                + f'<div class="task-freq-weekdays" style="flex:1;min-width:250px;display:{wd_style}">'
                  '<label class="param-label">选择星期（可多选）</label><div>' + wd_checks + "</div></div>"
                + f'<div class="task-freq-monthly" style="flex:1;min-width:150px;display:{md_style}">'
                  '<label class="param-label">每月几号</label>'
                  f'<input type="number" name="params[trigger_monthday]" min="1" max="31" value="{t_monthday}"></div>'
                + "</div>"
                + f'<div class="task-cd-format" style="display:{cd_fmt_style};flex-wrap:wrap;gap:10px;width:100%">'
                + '<div style="flex:1;min-width:260px"><label class="param-label">cron 表达式 *（5 段：分 时 日 月 周）</label>'
                  f'<input type="text" name="params[trigger_cron]" value="{esc(adv_cron)}" placeholder="0 9 * * *" required></div>'
                + "</div>"
                + "</div>"
                # 固定间隔
                + f'<div class="task-rm-interval" style="display:{rm_int_style};flex-wrap:wrap;gap:10px;width:100%">'
                + _field("trigger_interval", "间隔秒数 *", ts.get("interval_sec") or p.get("trigger_interval", ""),
                         "每 N 秒触发一次。3600=每小时，86400=每天", "number")
                + "</div>"
                # 公共：重复次数 + 延迟开始
                + _field("trigger_repeat_count", "重复次数（0=无限）", rep_cnt,
                         "执行满 N 次后任务自动结束（两种重复方式通用）", "number")
                + _field("trigger_offset_minutes", "延迟开始（分钟）", off_min,
                         "从节点执行时的当前时间起，N 分钟后开始首次执行", "number")
                + "</div>"
                # 单次模式：绝对时间（datetime-local）或相对当前时间 ± 分钟
                + f'<div class="task-mode-once" style="display:{once_style};flex-wrap:wrap;gap:10px;width:100%">'
                + '<div style="flex:1;min-width:220px"><label class="param-label">执行时间（年-月-日 时:分）</label>'
                  f'<input type="datetime-local" name="params[trigger_once_at]" value="{esc(once_val)}" step="60"></div>'
                + _field("trigger_once_offset_minutes", "或相对当前时间（分钟）", once_off,
                         "正数=延后、负数=提前，如 120 = 节点执行 2 小时后执行；与上方绝对时间二选一", "number")
                + "</div>"
                + "</div>"
            )
            # 触发配置：条件（condition）
            cond_type = ts.get("type") or p.get("trigger_cond_type", "friend_accepted")
            cond_target = ts.get("target")
            phone_val = (cond_target.get("phone", "") if isinstance(cond_target, dict) else "") or p.get("trigger_cond_phone", "")
            name_val = (cond_target.get("name", "") if isinstance(cond_target, dict)
                        else (cond_target if isinstance(cond_target, str) else "")) or p.get("trigger_cond_name", "")
            t_exp = p.get("trigger_expires_hours", "0")
            parts.append(
                '<div class="task-trigger-condition" style="display:flex;flex-wrap:wrap;gap:10px">'
                + _field("trigger_cond_type", "条件类型 *", str(cond_type), "", "select", True,
                         options=[("friend_accepted", "好友申请通过"), ("message_from", "收到某人消息")])
                + _field("trigger_cond_name", "对方昵称/备注", name_val,
                         "昵称或备注均可（企业微信显示名）；好友申请通过与收到消息均适用")
                + _field("trigger_cond_phone", "目标手机号（选填）", phone_val,
                         "仅好友申请通过：对方申请页显示电话时可按手机号匹配")
                + _field("trigger_expires_hours", "过期时间（小时，0=不过期）", t_exp,
                         "条件任务：创建后 N 小时仍未触发则自动停用", "number")
                + "</div>"
            )
            parts.append(
                _field("action_type", "动作类型 *", str(p.get("action_type", "workflow")), "", "select", True,
                       options=[("workflow", "执行工作流"), ("agent", "注入智能体指令")])
            )
            # 动作配置：执行工作流（workflow）—— 参数用键值表单行（避免手填 JSON）
            wf_param_items = []
            wf_params_dict = aspec.get("params")
            if not isinstance(wf_params_dict, dict):
                wf_params_dict = None
            if isinstance(wf_params_dict, dict):
                wf_param_items = list(wf_params_dict.items())[:8]
            wf_row_tmpl = (
                '<div class="wf-param-row" style="display:flex;gap:8px;align-items:center;margin-bottom:6px">'
                '<input type="text" name="params[action_wf_param_key][]" value="%s" placeholder="参数名" '
                'style="flex:1;min-width:120px">'
                '<input type="text" name="params[action_wf_param_val][]" value="%s" '
                'placeholder="值（支持 {{变量}}，如 {{input.user_text}}）" style="flex:2;min-width:180px">'
                '<button type="button" class="btn btn-ghost btn-sm wf-param-del" title="删除此行">✕</button></div>'
            )
            wf_rows = ""
            for k, v in wf_param_items:
                wf_rows += wf_row_tmpl % (esc(str(k)), esc(str(v)))
            wf_rows += (
                '<div class="wf-param-row" style="display:flex;gap:8px;align-items:center;margin-bottom:6px">'
                '<input type="text" name="params[action_wf_param_key][]" placeholder="参数名" '
                'style="flex:1;min-width:120px">'
                '<input type="text" name="params[action_wf_param_val][]" '
                'placeholder="值（支持 {{变量}}，如 {{input.user_text}}）" style="flex:2;min-width:180px">'
                '<button type="button" class="btn btn-ghost btn-sm wf-param-del" title="删除此行">✕</button></div>'
            )
            parts.append(
                '<div class="task-action-workflow" style="display:flex;flex-wrap:wrap;gap:10px">'
                + _field("action_workflow_id", "工作流 ID *", aspec.get("workflow_id") or p.get("action_workflow_id", ""),
                         "工作流管理页查看 ID", "number")
                + '<div style="flex:1;min-width:100%"><label class="param-label">工作流参数（选填，每行一个）</label>'
                + f'<div class="wf-param-box">{wf_rows}</div>'
                + '<button type="button" class="btn btn-outline btn-sm wf-param-add">＋ 添加参数</button></div>'
                + "</div>"
            )
            # 动作配置：注入智能体指令（agent）—— 先选执行后回复，再填回复会话名
            sr_raw = aspec.get("send_reply", True)
            if sr_raw is None:
                sr_raw = p.get("action_send_reply", "1")
            sr_str = "0" if str(sr_raw).strip().lower() in ("0", "false", "none") else "1"
            parts.append(
                '<div class="task-action-agent" style="display:flex;flex-wrap:wrap;gap:10px">'
                + _field("action_agent_id", "智能体 ID *", aspec.get("agent_id") or p.get("action_agent_id", ""),
                         "智能体管理页查看 ID", "number")
                + _field("action_instruction", "注入指令 *", aspec.get("instruction") or p.get("action_instruction", ""),
                         "发给智能体的指令文本，可用 {{变量}}", "textarea", True, span=2)
                + _field("action_send_reply", "执行后回复", sr_str, "", "select",
                         options=[("1", "回复"), ("0", "不回复")])
                + _field("action_room", "回复会话名（选填）", aspec.get("room") or p.get("action_room", ""),
                         "条件任务留空=回复到触发事件会话；定时任务无触发会话，建议显式填写会话名（如好友昵称），否则回复不会发送")
                + "</div>"
            )
            parts.append(
                _field("robot_id", "设备 ID robot_id", str(p.get("robot_id", "")),
                       "条件触发任务必填；可引用 {{input.robot_id}} 用当前设备")
            )
            parts.append(
                _field("enabled", "是否启用", str(p.get("enabled", "1")), "", "select",
                       options=[("1", "启用"), ("0", "停用")])
            )
            parts.append(_TASK_CREATE_SCRIPT)
        elif nt == "condition":
            parts.append(
                _field(
                    "field",
                    "判断字段 *",
                    p.get("field"),
                    "如 q.rows.0.status 或 input.user_text",
                    required=True,
                )
            )
            parts.append(
                _field(
                    "op",
                    "操作符",
                    str(p.get("op", "==")),
                    "",
                    "select",
                    options=CONDITION_OPS,
                )
            )
            parts.append(
                _field("value", "比较值", p.get("value"), "与判断字段比较的值")
            )
        elif nt == "switch":
            parts.append(
                _field(
                    "field",
                    "判断字段 *",
                    p.get("field"),
                    "如 score / q.rows.0.amount，取值后与各分支比较",
                    required=True,
                    span=3,
                )
            )
            parts.append(_switch_editor(p, node_names))
        elif nt == "code":
            parts.append(
                _field(
                    "code",
                    "Python 代码 *",
                    p.get("code"),
                    "往 output 字典写入键值即为节点输出；可用 get('a.b') 取上下文、data、ctx、json/math/re/random/datetime/time",
                    "textarea",
                    True,
                    rows=12,
                    span=3,
                )
            )
            parts.append(_CODE_HINT)
        elif nt == "output":
            parts.append(
                _field(
                    "text",
                    "回复文本模板 *",
                    p.get("text"),
                    "最终发给用户的回复，可用 {{变量}}",
                    "textarea",
                    True,
                    span=3,
                )
            )
            if node_info:
                parts.append(_output_refs_hint(node_info))
        blocks.append(
            f'<div class="nparams" data-ptype="{nt}" style="display:{"" if nt == node_type else "none"};flex-wrap:wrap;gap:10px">'
            + "".join(parts)
            + "</div>"
        )
    return "\n".join(blocks)


def _jump_select(
    field: str,
    value: str,
    node_names: list,
    empty_label: Optional[str] = "（中止）",
    allow_end: bool = False,
    auto_link: bool = False,
) -> str:
    """跳转目标下拉：一个空值选项 + （可选）「到此结束」 + 现有节点列表。

    `empty_label=None` = 不要空值选项。成功跳转就是这样：空 和 __end__ 是同一个意思，
    两个都给会渲染出两个「（到此结束）」，所以只保留 __end__ 这一个。
    """
    opts = "".join(
        f'<option value="{esc(nn)}"{(" selected" if nn == value else "")}>{esc(nn)}</option>'
        for nn in node_names
    )
    empty_opt = (
        f'<option value=""{" selected" if not value else ""}>{esc(empty_label)}</option>'
        if empty_label
        else ""
    )
    # 「新增节点」专用：默认去向 = 自动接在最后一个节点后面（由路由负责接线）
    auto_opt = (
        f'<option value="{AUTO_LINK}"{" selected" if value in ("", AUTO_LINK) else ""}>'
        "（自动接在最后）</option>"
        if auto_link
        else ""
    )
    end_opt = (
        f'<option value="{END_TARGET}"{" selected" if value == END_TARGET else ""}>'
        "（到此结束）</option>"
        if allow_end
        else ""
    )
    return (
        f'<select name="{field}">'
        f"{auto_opt}{empty_opt}{end_opt}{opts}</select>"
    )


def _node_form(
    action: str,
    node_type: str,
    p: dict,
    tables: list,
    name: str = "",
    ns: str = "",
    nf: str = "",
    node_names: list = None,
    node_info: list = None,
    agents: list = None,
    skills: list = None,
    auto_link: bool = False,
) -> str:
    """节点新增/编辑表单。ns/nf 为已保存的成功/失败跳转目标；agents 供 LLM 节点选择使用的智能体；skills 供 LLM 节点挂载技能。

    auto_link=True（新增节点）时，成功跳转多一个默认项「（自动接在最后）」。
    """
    type_opts = "".join(
        f'<option value="{nt}"{" selected" if nt == node_type else ""}>{label}</option>'
        for nt, label, _ in NODE_TYPES
    )
    node_names = node_names or []
    node_info = node_info or []
    # 跳转区随节点类型变：switch 用分支定义，其它节点用成功/失败跳转。
    # 两块都渲染出来，由 showParams() 按当前选中的类型切换——
    # 只按初始 node_type 渲染一块的话，把类型下拉切成"多分支判断"，
    # 屏幕上还留着成功/失败跳转（反过来也一样）。
    is_switch = node_type == "switch"
    jump_html = f"""
            <div class="njump" data-jswitch="1" style="flex:1;min-width:100%;display:{'flex' if is_switch else 'none'}">
                <div class="param-note note-warn">⚡ 多分支节点不需要「成功/失败跳转」，
                请在下方「分支定义」中为每个分支指定跳转节点；
                <b>每个分支都必须指定跳转目标</b>（留空 = 命中该分支就报错中止）。
                不想在下拉里选？点画布上这条分支线上的「＋」，直接给它建一个专属节点。</div>
            </div>
            <div class="njump" data-jswitch="0" style="gap:10px;flex-wrap:wrap;display:{'none' if is_switch else 'flex'}">
                <div style="width:170px">
                    <label class="param-label">成功跳转</label>
                    {_jump_select("next_on_success", ns, node_names, None, allow_end=True, auto_link=auto_link)}
                    <div class="form-hint">条件节点=满足时走此分支</div>
                </div>
                <div style="width:170px">
                    <label class="param-label">失败跳转</label>
                    {_jump_select("next_on_fail", nf, node_names, "（中止）")}
                    <div class="form-hint">条件节点=不满足时走此分支</div>
                </div>
            </div>"""
    return f"""
    <form method="POST" action="{action}" class="form-row" style="align-items:flex-end;gap:10px">
        <div style="display:flex;gap:10px;flex-wrap:wrap;flex:1">
            <div style="width:150px">
                <label class="param-label">节点类型</label>
                <select name="node_type" onchange="showParams(this)">{type_opts}</select>
            </div>
            <div style="width:180px">
                <label class="param-label">节点名</label>
                <input type="text" name="name" value="{esc(name)}" placeholder="留空自动生成 node_x">
            </div>
            {jump_html}
        </div>
        <div style="flex:1;min-width:100%;margin-top:10px">{_params_fields(node_type, p, tables, node_names, node_info, agents, skills)}</div>
        <button class="btn btn-primary btn-sm">保存节点</button>
    </form>
    """


# ════════════════════════════════════════════
#  工作流画布（可视化编排）
#  布局：按执行语义算最长路径分层，自顶向下（同层多节点左右并排）；
#        连边用 SVG 贝塞尔曲线，绿=成功/蓝紫=分支/红=失败/橙=兜底。
# ════════════════════════════════════════════

NODE_ICONS = {
    "llm": "🤖",
    "table_query": "🔍",
    "table_insert": "➕",
    "table_update": "✏️",
    "table_delete": "🗑️",
    "table_create": "🗄️",
    "http_get": "🌐",
    "http_post": "📤",
    "device_command": "📱",
    "task_create": "⏰",
    "condition": "❓",
    "switch": "⚡",
    "code": "🐍",
    "output": "💬",
}

_WF_NODE_W = 200
_WF_NODE_H = 76
_WF_COL_GAP = 46
_WF_ROW_GAP = 78
_WF_PAD = 44
# switch 分支配色（按分支序号循环）
_WF_BRANCH_COLORS = ["#4f46e5", "#0d9488", "#9333ea", "#2563eb", "#db2777", "#65a30d"]


def _wf_edges(order: list) -> list:
    """按引擎执行语义算出连边。

    与 workflow_engine 保持一致：非 switch 节点成功走 next_on_success（空/__end__=到此结束），
    失败走 next_on_fail（空=中止）；switch 节点走命中的分支，全不命中走 default_next。
    没有任何隐式边——**顺序不参与运行语义**，去向一律看节点自己写的跳转。
    dst=None 表示「流程结束 / 中止」。

    每条边还带：
    - branch: switch 分支在 params["branches"] 里的原始下标（兜底 = -1，非 switch = None）
    - open:   该端口没有可用目标（没写 或 目标已不存在）→ 画布上给它一个「＋」
    - dead:   目标名已不存在时的提示文案，否则 ""
    """
    index = {n["name"]: i for i, n in enumerate(order)}
    edges = []

    def _tgt(raw):
        """解析跳转目标名 → (下标, 死引用提示)；END_TARGET 是"到此结束"，不是死引用"""
        nm = str(raw or "").strip()
        if not nm or nm == END_TARGET:
            return None, ""
        if nm in index:
            return index[nm], ""
        return None, f"跳转目标「{nm}」已不存在"

    for i, n in enumerate(order):
        p = n.get("params") or {}
        if n["node_type"] == "switch":
            for bi, b in enumerate(p.get("branches") or []):
                if not isinstance(b, dict):
                    continue
                lbl = str(b.get("label") or "").strip()
                if not lbl:
                    continue
                raw = str(b.get("next") or "").strip()
                dst, dead = _tgt(raw)
                edges.append(
                    {
                        "src": i,
                        "dst": dst,
                        "color": _WF_BRANCH_COLORS[bi % len(_WF_BRANCH_COLORS)],
                        "label": lbl,
                        "dashed": False,
                        "branch": bi,
                        "open": not raw,
                        "ended": raw == END_TARGET,
                        "dead": dead,
                    }
                )
            raw = str(p.get("default_next") or "").strip()
            dst, dead = _tgt(raw)
            edges.append(
                {
                    "src": i,
                    "dst": dst,
                    "color": "#d97706",
                    "label": "兜底",
                    "dashed": True,
                    "branch": -1,
                    "open": not raw,
                    "ended": raw == END_TARGET,
                    "dead": dead,
                }
            )
        else:
            jump = str(n.get("next_on_success") or "").strip()
            if jump and jump != END_TARGET:
                dst, dead = _tgt(jump)
            else:
                dst, dead = None, ""  # 空 或 __end__ = 到此结束
            edges.append(
                {
                    "src": i,
                    "dst": dst,
                    "color": "#16a34a",
                    "label": "成功",
                    "dashed": False,
                    "branch": None,
                    "open": False,
                    "ended": dst is None,
                    "dead": dead,
                }
            )
            fail = str(n.get("next_on_fail") or "").strip()
            if fail:
                dst, dead = _tgt(fail)
                edges.append(
                    {
                        "src": i,
                        "dst": dst,
                        "color": "#dc2626",
                        "label": "失败",
                        "dashed": True,
                        "branch": None,
                        "open": False,
                        "ended": fail == END_TARGET,
                        "dead": dead,
                    }
                )
    return edges


def _wf_layout(order: list, edges: list):
    """分层分列布局 → (画布宽, 画布高, {下标: (x, y)})。

    层（y）：只按「向后」的边算最长路径，回跳边不参与——既让分层单调，也天然打断环。
    列（x）：主分支（第一条出边）沿用父列，旁支依次右移占新列——否则同列的长跳边
    会从中间节点身上穿过去，分支多的时候糊成一团。
    """
    n = len(order)
    level = [0] * n
    for _ in range(n):
        changed = False
        for e in edges:
            dst = e["dst"]
            if dst is None or dst <= e["src"]:
                continue
            if level[dst] < level[e["src"]] + 1:
                level[dst] = level[e["src"]] + 1
                changed = True
        if not changed:
            break

    col = [None] * n
    col[0] = 0
    next_col = 1
    for i in range(n):
        if col[i] is None:
            col[i] = 0  # 不可达节点（前驱没连过来）放主列
        outs = [e for e in edges if e["src"] == i and e["dst"] is not None]
        for k, e in enumerate(outs):
            j = e["dst"]
            if j <= i or col[j] is not None:
                continue  # 回跳边不占列；已定列的节点不抢
            if k == 0:
                col[j] = col[i]
            else:
                col[j] = next_col
                next_col += 1

    # 兜底占位：switch 的分支还没接目标时，它后面的节点没有任何入边 → 会和它算到同一格，
    # 两张卡片直接叠在一起。按先后顺序把重复格位的节点往下推，保证不重叠。
    level = _wf_resolve_collisions(level, col)
    step_x = _WF_NODE_W + _WF_COL_GAP
    step_y = _WF_NODE_H + _WF_ROW_GAP
    width = _WF_PAD * 2 + max(col) * step_x + _WF_NODE_W
    height = _WF_PAD * 2 + max(level) * step_y + _WF_NODE_H
    pos = {
        i: (_WF_PAD + col[i] * step_x, _WF_PAD + level[i] * step_y) for i in range(n)
    }
    return width, height, pos


def _wf_resolve_collisions(level: list, col: list) -> list:
    """同一 (层, 列) 上有多个节点时，后到的往下推，避免卡片重叠"""
    level = list(level)
    taken = set()
    for i in range(len(level)):
        key = (level[i], col[i])
        while key in taken:
            level[i] += 1
            key = (level[i], col[i])
        taken.add(key)
    return level


def _wf_bezier(sx, sy, tx, ty, off: float = 0.0):
    """连边路径 + 控制点（调用方按 t 取标签/按钮坐标）。

    off：同源的多条出边按分支序号左右分道，让每条分支的标签和「＋」都落在自己那条线上，
    不会跑到别的分支的线上。回跳（目标在上方）向右外扩绕行。
    ponytail: 回跳边固定向右鼓包，重叠严重时再上真正的正交布线算法
    """
    if ty <= sy:
        cx = max(sx, tx) + 140 + off
        pts = ((sx, sy), (cx, sy + 40), (cx, ty - 40), (tx, ty))
    else:
        dy = (ty - sy) / 2
        pts = ((sx, sy), (sx + off, sy + dy), (tx + off, ty - dy), (tx, ty))
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts
    d = (
        f"M {x0:.0f} {y0:.0f} C {x1:.0f} {y1:.0f}, "
        f"{x2:.0f} {y2:.0f}, {x3:.0f} {y3:.0f}"
    )
    return d, pts


def _wf_bezier_pt(pts, t: float):
    """三次贝塞尔上 t 处的坐标"""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts
    u = 1 - t
    return (
        u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
        u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3,
    )


_WF_CANVAS_CSS = """<style>
.wf-toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.wf-toolbar .grow{flex:1}
.wf-viewport{
  position:relative;height:min(66vh,640px);min-height:420px;overflow:hidden;
  background:#fbfcfe;border:1px solid var(--border);border-radius:var(--radius);
  background-image:radial-gradient(#dfe4ec 1px,transparent 1px);background-size:22px 22px;
  cursor:grab;user-select:none;
}
.wf-viewport.wf-panning{cursor:grabbing}
.wf-stage{position:absolute;left:0;top:0;transform-origin:0 0}
.wf-svg{position:absolute;left:0;top:0;overflow:visible;pointer-events:none}
.wf-elabel{font-size:11px;font-family:inherit;paint-order:stroke;stroke:#fff;stroke-width:3px;stroke-linejoin:round}
.wf-node{
  position:absolute;width:200px;height:76px;box-sizing:border-box;padding:9px 12px;
  background:var(--surface);border:1.5px solid var(--border-strong);border-left:4px solid var(--primary);
  border-radius:10px;box-shadow:0 1px 3px rgba(15,23,42,.09);cursor:pointer;
  display:flex;flex-direction:column;justify-content:center;gap:3px;transition:box-shadow .15s,border-color .15s;
}
.wf-node:hover{box-shadow:0 6px 18px rgba(79,70,229,.22);border-color:var(--primary)}
.wf-node.wf-open{border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-bg)}
.wf-node-hd{display:flex;align-items:center;gap:6px;font-size:13px;overflow:hidden}
.wf-node-hd b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wf-node-bd{font-size:11px;color:var(--text-3);display:flex;align-items:center;gap:6px}
.wf-node-idx{
  position:absolute;right:8px;top:7px;font-size:10px;color:var(--text-3);
  background:#f1f5f9;border-radius:6px;padding:0 5px
}
.wf-node.wf-t-llm{border-left-color:#6366f1}
.wf-node.wf-t-table_query,.wf-node.wf-t-table_create,.wf-node.wf-t-table_insert,
.wf-node.wf-t-table_update,.wf-node.wf-t-table_delete{border-left-color:#0891b2}
.wf-node.wf-t-http_get,.wf-node.wf-t-http_post{border-left-color:#0d9488}
.wf-node.wf-t-device_command{border-left-color:#ea580c}
.wf-node.wf-t-task_create{border-left-color:#b45309}
.wf-node.wf-t-condition{border-left-color:#7c3aed}
.wf-node.wf-t-switch{border-left-color:#9333ea}
.wf-node.wf-t-code{border-left-color:#334155}
.wf-node.wf-t-output{border-left-color:#16a34a}
.wf-add{
  position:absolute;left:50%;bottom:-11px;transform:translateX(-50%);z-index:2;
  width:22px;height:22px;padding:0;border-radius:50%;cursor:pointer;
  border:1.5px solid var(--primary);background:var(--surface);color:var(--primary);
  font-size:13px;line-height:1;opacity:0;transition:opacity .15s
}
.wf-node:hover .wf-add{opacity:1}
.wf-add:hover{background:var(--primary);color:#fff}
.wf-edgeadd{
  position:absolute;width:20px;height:20px;padding:0;z-index:3;cursor:pointer;
  transform:translate(-50%,-50%);border-radius:50%;border:1.5px dashed var(--primary);
  background:var(--surface);color:var(--primary);font-size:12px;line-height:1;opacity:.75
}
.wf-edgeadd:hover{background:var(--primary);color:#fff;border-style:solid;opacity:1}
.wf-start{
  position:absolute;left:-1px;top:-10px;z-index:2;height:19px;line-height:19px;padding:0 8px;
  border-radius:9px;background:var(--green);color:#fff;font-size:10px;font-weight:600;white-space:nowrap
}
.wf-node.wf-is-start{border-color:var(--green)}
.wf-empty{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);color:var(--text-3)}
.wf-drawer{
  position:fixed;top:0;right:0;bottom:0;width:min(620px,94vw);z-index:90;
  background:var(--surface);border-left:1px solid var(--border);box-shadow:-8px 0 32px rgba(15,23,42,.16);
  display:flex;flex-direction:column;transform:translateX(102%);transition:transform .18s ease;
}
.wf-drawer.wf-show{transform:none}
.wf-drawer-hd{
  display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border);
  background:#f8fafc;border-radius:0
}
.wf-drawer-hd b{flex:1;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wf-drawer-ops{display:flex;gap:8px}
.wf-drawer-body{padding:14px 16px 40px;overflow:auto;flex:1}
.wf-drawer-body .form-row{flex-wrap:wrap}
.wf-hint{font-size:12px;color:var(--text-3)}
.wf-toast{
  position:fixed;right:24px;bottom:24px;z-index:120;max-width:min(420px,80vw);
  background:var(--surface);border:1px solid var(--border);border-left:4px solid var(--green);
  border-radius:10px;box-shadow:0 8px 28px rgba(15,23,42,.18);padding:10px 16px;font-size:13px;
  transition:opacity .5s;pointer-events:none
}
.wf-toast-err{border-left-color:var(--red)}
</style>"""

_WF_CANVAS_JS = r"""<script>
var WF_ID = @@WF_ID@@, WF_W = @@WF_W@@, WF_H = @@WF_H@@;
var WF_NODEW = @@WF_NODEW@@, WF_NODEH = @@WF_NODEH@@;
var WF_KEY = 'wf-edit-' + WF_ID;
var wfView = {x:0, y:0, s:1}, WF_OPEN = null, wfDirty = false;

function wfApply(){
    var st = document.getElementById('wfStage');
    st.style.transform = 'translate(' + wfView.x + 'px,' + wfView.y + 'px) scale(' + wfView.s + ')';
}
function wfFit(){
    var vp = document.getElementById('wfViewport').getBoundingClientRect();
    // 下限 0.55：长流程宁可让用户平移，也不要把字缩到看不清
    var s = Math.max(0.55, Math.min(1, Math.min((vp.width - 32) / WF_W, (vp.height - 32) / WF_H)));
    if (!isFinite(s) || s <= 0) s = 1;
    wfView.s = s;
    wfView.x = Math.max(12, (vp.width - WF_W * s) / 2);
    wfView.y = 16;
    wfApply();
}
function wfCenter(nid){
    var el = document.querySelector('.wf-node[data-nid="' + nid + '"]');
    if (!el) return;
    var vp = document.getElementById('wfViewport').getBoundingClientRect();
    // 抽屉占住右侧，把节点居中在可视区内
    var drawer = document.getElementById('wfDrawer');
    var avail = vp.width - (drawer && drawer.classList.contains('wf-show') ? drawer.offsetWidth : 0);
    var cx = parseFloat(el.style.left) * wfView.s, cy = parseFloat(el.style.top) * wfView.s;
    wfView.x = avail / 2 - cx - (WF_NODEW * wfView.s) / 2;
    wfView.y = vp.height / 2 - cy - (WF_NODEH * wfView.s) / 2;
    wfApply();
}

function wfOpen(nid, skipConfirm){
    if (WF_OPEN === nid) { wfShow(); return; }
    if (wfDirty && !skipConfirm && !confirm('当前节点的修改还没保存，确定切换吗？')) return;
    if (WF_OPEN !== null) {
        var cur = document.querySelector('#wfDrawerBody .wf-slot');
        if (cur) document.getElementById('wfForms').appendChild(cur);
    }
    var slot = document.querySelector('#wfForms .wf-slot[data-nid="' + nid + '"]');
    if (!slot) return;
    document.getElementById('wfDrawerBody').appendChild(slot);
    document.getElementById('wfDrawerTitle').textContent =
        (nid === 0 ? '➕ 新增节点' : '✏️ ' + (slot.getAttribute('data-name') || '编辑节点'));
    WF_OPEN = nid;
    wfDirty = false;
    wfShow();
    var sel = slot.querySelector('select[name="node_type"]');
    if (sel && window.showParams) showParams(sel);
    slot.querySelectorAll('.CodeMirror').forEach(function(ce){ if (ce.CodeMirror) ce.CodeMirror.refresh(); });
    document.querySelectorAll('.wf-node').forEach(function(el){
        el.classList.toggle('wf-open', el.getAttribute('data-nid') === String(nid));
    });
    wfCenter(nid);
}
function wfShow(){
    document.getElementById('wfDrawer').classList.add('wf-show');
}
function wfClose(){
    if (wfDirty && !confirm('当前节点的修改还没保存，确定关闭吗？')) return;
    var cur = document.querySelector('#wfDrawerBody .wf-slot');
    if (cur) document.getElementById('wfForms').appendChild(cur);
    WF_OPEN = null; wfDirty = false;
    document.getElementById('wfDrawer').classList.remove('wf-show');
    document.querySelectorAll('.wf-node.wf-open').forEach(function(el){ el.classList.remove('wf-open'); });
}

// —— 平移 / 缩放 ——
(function(){
    var vp = document.getElementById('wfViewport');
    var drag = null;
    vp.addEventListener('mousedown', function(e){
        if (e.target.closest('.wf-node')) return;
        drag = {x:e.clientX - wfView.x, y:e.clientY - wfView.y};
        vp.classList.add('wf-panning');
    });
    window.addEventListener('mousemove', function(e){
        if (!drag) return;
        wfView.x = e.clientX - drag.x; wfView.y = e.clientY - drag.y; wfApply();
    });
    window.addEventListener('mouseup', function(){ drag = null; vp.classList.remove('wf-panning'); });
    vp.addEventListener('wheel', function(e){
        e.preventDefault();
        var r = vp.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
        var ns = Math.min(2, Math.max(0.35, wfView.s * (e.deltaY < 0 ? 1.12 : 1/1.12)));
        var k = ns / wfView.s;
        wfView.x = mx - (mx - wfView.x) * k;
        wfView.y = my - (my - wfView.y) * k;
        wfView.s = ns; wfApply();
    }, {passive:false});
})();

// —— 脏标记：抽屉内任何输入都算改动 ——
['input','change'].forEach(function(ev){
    document.addEventListener(ev, function(e){
        if (e.target.closest && e.target.closest('#wfDrawerBody')) wfDirty = true;
    }, true);
});

// —— 记住滚动位置与打开的节点，整页提交回来后恢复 ——
// 注意：整站的滚动容器是 .content（overflow-y:auto），window 根本不滚，
// 所以 window.scrollY / window.scrollTo 在这里永远是 0 —— 必须操作 .content。
function wfScroller(){ return document.querySelector('.content'); }
function wfScrollTop(){
    var s = wfScroller();
    return s ? s.scrollTop : (window.scrollY || 0);
}
function wfScrollTo(y){
    var s = wfScroller();
    if (s) s.scrollTop = y; else window.scrollTo(0, y);
}
function wfPersist(){
    try {
        sessionStorage.setItem(WF_KEY, JSON.stringify(
            {y:wfScrollTop(), open:WF_OPEN, s:wfView.s, x:wfView.x, vy:wfView.y}
        ));
    } catch(e){}
}
window.addEventListener('beforeunload', wfPersist);
// 顶部提示条离视口太远，浮一个 toast，避免为了看提示而跳回顶部
function wfToast(){
    var b = document.querySelector('.msg');
    if (!b) return;
    var t = document.createElement('div');
    t.className = 'wf-toast' + (b.className.indexOf('msg-error') >= 0 ? ' wf-toast-err' : '');
    t.textContent = b.textContent;
    document.body.appendChild(t);
    setTimeout(function(){ t.style.opacity = '0'; }, 2600);
    setTimeout(function(){ t.remove(); }, 3300);
}
// 页面末尾的脚本会初始化 CodeMirror / showParams，等它跑完再恢复抽屉
window.addEventListener('DOMContentLoaded', function(){
    var st = null;
    try { st = JSON.parse(sessionStorage.getItem(WF_KEY) || 'null'); } catch(e){}
    var open = st && st.open ? st.open : null;
    // 刚添加完的节点：服务端回显 "✅ 节点已添加 (#12)"，直接打开它
    var banner = document.querySelector('.msg');
    var mm = banner && banner.textContent.match(/节点已添加\s*\(#(\d+)\)/);
    if (mm) open = parseInt(mm[1], 10);
    wfFit();
    if (st && st.s) { wfView.s = st.s; wfView.x = st.x; wfView.y = st.vy; wfApply(); }
    if (open) wfOpen(open, true);
    if (st && st.y) wfScrollTo(st.y);
    wfToast();
});
</script>"""


def _wf_canvas(
    wf: dict,
    workflow_id: int,
    oid_q: str,
    oid_amp: str,
    tables: list,
    agents: list,
    skills: list,
) -> str:
    """可视化编排画布：节点图 + 抽屉式编辑面板（表单复用 _node_form）"""
    order = sorted(wf["nodes"], key=lambda n: n["id"])  # 顺序只影响"谁是第一个节点"
    edges = _wf_edges(order)
    width, height, pos = _wf_layout(order, edges) if order else (460, 200, {})
    node_names = [n["name"] for n in order]
    node_info = [(n["name"], n["node_type"]) for n in order]

    # ── 连边（SVG）+ 终端标记 + 分支「＋」热点 ──
    paths, labels, adders = [], [], []
    used_label_pos: set = set()
    used_adder_pos: set = set()
    node_ids = [n["id"] for n in order]

    def _dedup(x: float, y: float, used: set, dx: int, dy: int):
        """同位置多条边时错开（如成功/失败跳同一节点）

        ponytail: 只挪标签/按钮不挪曲线，曲线本身还叠着；真嫌糊再上平行偏移布线
        """
        key = (round(x), round(y))
        while key in used:
            key = (key[0] + dx, key[1] + dy)
        used.add(key)
        return key

    def _add_url(src_idx: int, branch: int) -> str:
        return (
            f"/admin/workflow/{workflow_id}/node/{node_ids[src_idx]}/insert"
            f"?branch={branch}{oid_amp}"
        )

    # 同源的每条出边各占一条"道"：switch 有几个分支就横向分开几条线，
    # 这样每条分支的标签和「＋」永远落在自己那条线上，不会跑到别人线上。
    out_total: dict = {}
    for e in edges:
        out_total[e["src"]] = out_total.get(e["src"], 0) + 1
    out_seen: dict = {}

    for e in edges:
        i = e["src"]
        k = out_seen.get(i, 0)
        out_seen[i] = k + 1
        spread = k - (out_total[i] - 1) / 2
        off = spread * 64
        sx = pos[i][0] + _WF_NODE_W / 2
        sy = pos[i][1] + _WF_NODE_H
        tip = f"（{esc(e['dead'])}）" if e["dead"] else ""
        if e["dst"] is None:
            # 这条分支到此为止（没设目标 / 目标被删了）：从节点底部引一小段到本道位置。
            # 标签右对齐 + 「＋」并排，整体落在两行节点之间的 78px 缝里，
            # 否则「＋」会压到下一张卡片上（看起来像是那条分支上的按钮）。
            lx, ly = sx + off, sy + 40
            paths.append(
                f'<path d="M {sx:.0f} {sy:.0f} L {lx:.0f} {ly - 12:.0f}" fill="none" '
                f'stroke="{e["color"]}" stroke-width="1.6" stroke-dasharray="4 3" opacity="0.85"/>'
            )
            # 用文字而不是符号：⏹ 这类字符在部分系统字体里是豆腐块
            if e["dead"]:
                mark = "⚠"
            elif e["ended"]:
                mark = "结束"
            elif e["open"]:
                mark = "中止"
            else:
                mark = "结束"
            labels.append(
                f'<text x="{lx - 14:.0f}" y="{ly:.0f}" text-anchor="end" dominant-baseline="middle" '
                f'class="wf-elabel" fill="{e["color"]}">{esc(e["label"])} {mark}'
                f'<title>{esc(e["label"])}{tip}</title></text>'
            )
            ax, ay = lx + 4, ly
        else:
            tx = pos[e["dst"]][0] + _WF_NODE_W / 2
            ty = pos[e["dst"]][1]
            d, pts = _wf_bezier(sx, sy, tx, ty, off)
            lx, ly = _dedup(*_wf_bezier_pt(pts, 0.5), used_label_pos, 0, 15)
            ax, ay = _dedup(*_wf_bezier_pt(pts, 0.76), used_adder_pos, 0, 24)
            dash = ' stroke-dasharray="6 4"' if e["dashed"] else ""
            paths.append(
                f'<path d="{d}" fill="none" stroke="{e["color"]}" stroke-width="1.8"{dash} '
                f'marker-end="url(#wfArrow{e["color"].lstrip("#")})"/>'
            )
            labels.append(
                f'<text x="{lx:.0f}" y="{ly:.0f}" text-anchor="middle" dominant-baseline="middle" '
                f'class="wf-elabel" fill="{e["color"]}">{esc(e["label"])}'
                f'<title>{esc(e["label"])}{tip}</title></text>'
            )
        # 分支/兜底没有可用目标 → 在它自己那条线上给个「＋」，点它给这条分支单独挂新节点
        if e["open"] and e["branch"] is not None:
            who = "兜底" if e["branch"] == -1 else f"分支「{esc(e['label'])}」"
            what = "重新挂一个节点" if e["dead"] else "新建一个专属节点"
            adders.append(
                f'<button type="button" class="wf-edgeadd" style="left:{ax:.0f}px;top:{ay:.0f}px" '
                f'title="给{who}{what}" '
                f"onclick=\"event.stopPropagation();location.href='{_add_url(e['src'], e['branch'])}'\">＋</button>"
            )
    colors = sorted({e["color"] for e in edges})
    markers = "".join(
        f'<marker id="wfArrow{c.lstrip("#")}" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 1 L 10 5 L 0 9 z" fill="{c}"/></marker>'
        for c in colors
    )

    # ── 节点卡片 ──
    # 起点 = id 最小的节点（引擎从它开始跑），它不允许删除，所以必须标出来
    start_id = order[0]["id"] if order else None
    boxes = []
    for i, n in enumerate(order):
        label, _desc = NODE_LABELS.get(n["node_type"], (n["node_type"], ""))
        x, y = pos[i]
        is_start = n["id"] == start_id
        start_badge = (
            '<span class="wf-start" title="起点：工作流从这里开始跑，不能删除">▶ 起点</span>'
            if is_start
            else ""
        )
        # switch 的走向由各分支决定，底下再挂一个「＋」只会让人误以为那是它的"下一个"，
        # 所以 switch 节点不给底部「＋」——挂节点走各分支线上的「＋」
        add_btn = (
            ""
            if n["node_type"] == "switch"
            else (
                f'<button type="button" class="wf-add" title="在它下面插入节点（顺序续接）" '
                f"onclick=\"event.stopPropagation();location.href="
                f"'/admin/workflow/{workflow_id}/node/{n['id']}/insert{oid_q}'\">＋</button>"
            )
        )
        boxes.append(
            f'<div class="wf-node wf-t-{esc(n["node_type"])}{" wf-is-start" if is_start else ""}" '
            f'data-nid="{n["id"]}" '
            f'data-name="{esc(n["name"])}" style="left:{x:.0f}px;top:{y:.0f}px" '
            f'onclick="wfOpen({n["id"]})" title="{esc(n["name"])} · {esc(label)}">'
            f"{start_badge}"
            f'<span class="wf-node-idx">{i + 1}</span>'
            f'<div class="wf-node-hd"><span>{NODE_ICONS.get(n["node_type"], "🔧")}</span>'
            f'<b>{esc(n["name"])}</b></div>'
            f'<div class="wf-node-bd">{esc(label)}</div>'
            f"{add_btn}"
            f'</div>'
        )

    stage_inner = (
        f'<svg class="wf-svg" width="{width:.0f}" height="{height:.0f}"><defs>{markers}</defs>'
        + "".join(paths)
        + "".join(labels)
        + "</svg>"
        + "".join(boxes)
        + "".join(adders)
        if order
        else '<div class="wf-empty">还没有节点 —— 点右上角「+ 添加节点」开始编排</div>'
    )

    # ── 抽屉里的表单（隐藏存放，点节点时搬进抽屉）──
    slots = []
    for n in order:
        ins = (
            ""
            if n["node_type"] == "switch"
            else f'<a href="/admin/workflow/{workflow_id}/node/{n["id"]}/insert{oid_q}" class="btn btn-ghost btn-sm">＋ 在下方插入</a>'
        )
        slots.append(
            f'<div class="wf-slot" data-nid="{n["id"]}" data-name="{esc(n["name"])}">'
            f'<div class="wf-drawer-ops" style="margin-bottom:12px">'
            f"{ins}"
            + (
                '<span class="wf-hint" style="align-self:center">'
                "▶ 起点节点不能删除（工作流从这里开始跑）</span>"
                if n["id"] == start_id
                else (
                    f'<a href="/admin/workflow/{workflow_id}/node/{n["id"]}/delete{oid_q}" '
                    f'class="btn btn-danger btn-sm" '
                    f"onclick=\"return confirm('删除节点 {esc(n['name'])}？')\">🗑 删除</a>"
                )
            )
            + f"</div>"
            f"{_node_form(f'/admin/workflow/{workflow_id}/node/{n["id"]}/update{oid_q}', n['node_type'], n['params'], tables, n['name'], n['next_on_success'], n['next_on_fail'], node_names, node_info, agents, skills)}"
            f"</div>"
        )
    slots.append(
        f'<div class="wf-slot" data-nid="0" data-name="">'
        f"{_node_form(f'/admin/workflow/{workflow_id}/node/add{oid_q}', 'llm', {}, tables, node_names=node_names, node_info=node_info, agents=agents, skills=skills, auto_link=True)}"
        f"</div>"
    )

    js = (
        _WF_CANVAS_JS.replace("@@WF_ID@@", str(workflow_id))
        .replace("@@WF_W@@", f"{width:.0f}")
        .replace("@@WF_H@@", f"{height:.0f}")
        .replace("@@WF_NODEW@@", str(_WF_NODE_W))
        .replace("@@WF_NODEH@@", str(_WF_NODE_H))
    )

    return f"""
    {_WF_CANVAS_CSS}
    <div class="wf-toolbar">
        <button type="button" class="btn btn-primary btn-sm" onclick="wfOpen(0)">＋ 添加节点（追加到末尾）</button>
        <button type="button" class="btn btn-ghost btn-sm" onclick="wfFit()">适应窗口</button>
        <span class="grow"></span>
        <span class="wf-hint">点节点编辑 · 节点下「＋」顺序插节点 · 分支线上的「＋」给该分支挂新节点 · 滚轮缩放</span>
    </div>
    <div class="wf-viewport" id="wfViewport">
        <div class="wf-stage" id="wfStage" style="width:{width:.0f}px;height:{height:.0f}px">{stage_inner}</div>
    </div>
    <aside class="wf-drawer" id="wfDrawer">
        <div class="wf-drawer-hd">
            <b id="wfDrawerTitle">编辑节点</b>
            <button type="button" class="btn btn-ghost btn-sm" onclick="wfClose()">✕ 关闭</button>
        </div>
        <div class="wf-drawer-body" id="wfDrawerBody"></div>
    </aside>
    <div id="wfForms" hidden>{"".join(slots)}</div>
    {js}
    """


# ════════════════════════════════════════════
#  工作流列表 / 创建
# ════════════════════════════════════════════


@router.get("/admin/workflows")
async def admin_workflows(msg: str = "", org_id: int = 0):
    """工作流列表页（支持 ?org_id= 按组织过滤）"""
    all_workflows = await workflow_service.list_workflows()
    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if not grp:
            return RedirectResponse(
                url="/admin/orgs?msg=❌ 组织不存在", status_code=303
            )
        cur_org_name = grp["name"]
        member_ids = set(grp["members"].get("workflow", []))
        all_workflows = [w for w in all_workflows if str(w.id) in member_ids]

    workflows = all_workflows
    workflow_groups = await group_service.list_org_groups()
    group_name = {g["id"]: g["name"] for g in workflow_groups}

    rows = []
    for w in workflows:
        sel_groups = set(await group_service.get_member_org_groups("workflow", w.id))
        groups_text = "、".join(
            esc(group_name[gid]) for gid in sorted(sel_groups) if gid in group_name
        )
        rows.append(
            f"<tr>"
            f"<td><code>#{w.id}</code></td>"
            f"<td><span class='cell-main'>{esc(w.name)}</span><span class='cell-sub'>{esc(w.description)}</span></td>"
            f"<td>{w.node_count}</td>"
            f"<td>{groups_text or '—'}</td>"
            f"<td>{'<span class="badge badge-online">启用</span>' if w.enabled else '<span class="badge badge-offline">停用</span>'}"
            f"{'<span class="badge" style="background:#fef3c7;color:#b45309" title="高风险操作：授权给对话智能体时请谨慎">⚠️敏感</span>' if getattr(w, 'is_sensitive', False) else ''}</td>"
            f"<td>{_fmt_dt(w.created_at)}</td>"
            f"<td>"
            f'<a href="/admin/workflow/{w.id}{f"?org_id={org_id}" if org_id else ""}" class="btn btn-primary btn-sm">编排</a> '
            f'<form method="POST" action="/admin/workflow/{w.id}/toggle{f"?org_id={org_id}" if org_id else ""}" class="inline-form">'
            f'<button class="btn btn-ghost btn-sm">{("停用" if w.enabled else "启用")}</button></form> '
            f'<a href="/admin/workflow/{w.id}/delete{f"?org_id={org_id}" if org_id else ""}" class="btn btn-danger btn-sm" '
            f"onclick=\"return confirm('删除工作流「{esc(w.name)}」？节点与运行日志将一并删除')\">删除</a>"
            f"</td></tr>"
        )
    rows_html = (
        "\n".join(rows)
        if rows
        else '<tr><td colspan="7" class="empty">暂无工作流，先在上方创建</td></tr>'
    )

    content = f"""
    <div class="card">
        <h3>⚙️ 新建工作流</h3>
        <p class="card-sub">工作流 = 按顺序执行的节点序列。触发词/命令路由尚未接入，当前可「测试运行」验证流程。</p>
        <form method="POST" action="/admin/workflow/create{f'?org_id={org_id}' if org_id else ''}" class="form-row" style="align-items:flex-start">
            <div class="form-group" style="flex:1"><label>工作流名称 *</label><input type="text" name="name" placeholder="如：查物流" required></div>
            <div class="form-group" style="flex:2"><label>描述</label><input type="text" name="description" placeholder="这条工作流做什么"></div>
            <div class="form-group"><label>超时(秒)</label><input type="number" name="timeout_sec" value="60" style="width:90px"></div>
            <div class="form-group">
                <label class="param-label">敏感标记</label>
                <label class="form-check" style="margin-top:6px">
                    <input type="checkbox" name="is_sensitive" value="1">
                    <span style="color:#b45309" title="涉及删除/导出/外部副作用等高风险操作时开启；授权给对话智能体时管理后台会警告（仅提示与审计）">⚠️ 高风险工作流</span>
                </label>
            </div>
            <button class="btn btn-primary">创建</button>
        </form>
        <p class="card-sub" style="margin-top:8px">
            📥 <b>输入参数声明</b>（可选）：创建后进入编排页，用表格表单声明工作流需要的输入字段（如 <code>user_text</code> 必填），
            智能体运行此工作流时按声明传参。<code>user_text</code> 自动取当前消息文本，未声明即表示工作流不依赖用户消息。
        </p>
    </div>
    <div class="card">
        <h3>📋 工作流列表</h3>
        <div class="table-wrap">
        <table>
            <thead><tr><th>ID</th><th>名称</th><th>节点数</th><th>组织</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
        </div>
    </div>
    """
    return page(
        "工作流",
        "🔄 工作流管理",
        content,
        msg,
        active="workflows",
        org_id=org_id,
        org_name=cur_org_name,
        org_active="workflows",
    )


@router.post("/admin/workflow/create")
async def admin_workflow_create(
    org_id: int = Query(0),
    name: str = Form(...),
    description: str = Form(""),
    timeout_sec: int = Form(60),
    is_sensitive: bool = Form(False),
    input_schema: str = Form(""),
):
    try:
        wf_id = await workflow_service.create_workflow(
            WorkflowCreateRequest(
                name=name,
                description=description,
                timeout_sec=timeout_sec,
                is_sensitive=is_sensitive,
                input_schema=_validate_input_schema(input_schema),
            )
        )
        # 在组织上下文中创建则自动归属该组织（创建后不可改）
        if org_id:
            await group_service.set_member_org_groups("workflow", wf_id, [org_id])
        base = (
            f"/admin/workflow/{wf_id}?org_id={org_id}"
            if org_id
            else f"/admin/workflow/{wf_id}"
        )
        return RedirectResponse(
            url=_with_msg(base, "✅ 工作流已创建，开始编排节点"), status_code=303
        )
    except Exception as e:
        base = f"/admin/workflows?org_id={org_id}" if org_id else "/admin/workflows"
        return RedirectResponse(url=_with_msg(base, f"❌ {esc(str(e))}"), status_code=303)


@router.get("/admin/workflow/{workflow_id}/delete")
async def admin_workflow_delete(workflow_id: int, org_id: int = Query(0)):
    ok = await workflow_service.delete_workflow(workflow_id)
    base = f"/admin/workflows?org_id={org_id}" if org_id else "/admin/workflows"
    return RedirectResponse(
        url=_with_msg(base, "✅ 已删除" if ok else "❌ 工作流不存在"), status_code=303
    )


@router.post("/admin/workflow/{workflow_id}/toggle")
async def admin_workflow_toggle(workflow_id: int, org_id: int = Query(0)):
    wf = await workflow_service.get_workflow(workflow_id)
    if wf:
        await workflow_service.set_enabled(workflow_id, not wf["enabled"])
    base = f"/admin/workflows?org_id={org_id}" if org_id else "/admin/workflows"
    return RedirectResponse(url=base, status_code=303)


@router.post("/admin/workflow/{workflow_id}/sensitive")
async def admin_workflow_sensitive(workflow_id: int, org_id: int = Query(0)):
    """切换敏感标记（高风险工作流，授权对话智能体时管理后台警告）"""
    wf = await workflow_service.get_workflow(workflow_id)
    if wf:
        await workflow_service.set_sensitive(workflow_id, not wf["is_sensitive"])
    base = (
        f"/admin/workflow/{workflow_id}?org_id={org_id}"
        if org_id
        else f"/admin/workflow/{workflow_id}"
    )
    return RedirectResponse(url=base, status_code=303)


@router.post("/admin/workflow/{workflow_id}/input_schema")
async def admin_workflow_input_schema(
    workflow_id: int,
    request: Request,
    org_id: int = Query(0),
):
    """保存工作流输入参数声明（表单式：sc_name/sc_type/sc_required/sc_desc 数组）"""
    base = (
        f"/admin/workflow/{workflow_id}?org_id={org_id}"
        if org_id
        else f"/admin/workflow/{workflow_id}"
    )
    try:
        form = await request.form()
        names = form.getlist("sc_name[]")
        types = form.getlist("sc_type[]")
        reqs = form.getlist("sc_required[]")
        descs = form.getlist("sc_desc[]")
        schema = []
        for i, name in enumerate(names):
            name = (name or "").strip()
            if not name:
                continue
            schema.append(
                {
                    "name": name,
                    "type": types[i] if i < len(types) else "string",
                    "required": (reqs[i] if i < len(reqs) else "0") == "1",
                    "description": descs[i] if i < len(descs) else "",
                }
            )
        normalized = json.dumps(schema, ensure_ascii=False) if schema else None
        ok = await workflow_service.update_input_schema(workflow_id, normalized)
        if not ok:
            return RedirectResponse(url=_with_msg(base, "❌ 工作流不存在"), status_code=303)
        return RedirectResponse(url=_with_msg(base, "✅ 输入参数声明已保存"), status_code=303)
    except Exception as e:
        return RedirectResponse(url=_with_msg(base, f"❌ {esc(str(e))}"), status_code=303)


# ════════════════════════════════════════════
#  工作流详情：节点编排 + 测试运行 + 日志
# ════════════════════════════════════════════


@router.get("/admin/workflow/{workflow_id}")
async def admin_workflow_detail(workflow_id: int, msg: str = "", org_id: int = 0):
    wf = await workflow_service.get_workflow(workflow_id)
    if not wf:
        return RedirectResponse(
            url="/admin/workflows?msg=❌ 工作流不存在", status_code=303
        )
    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if grp:
            cur_org_name = grp["name"]
    # 组织隔离：节点/测试运行中可选的表格、设备仅限本组织
    org_table_ids = await _org_scoped_ids(org_id, "table")
    all_tables = await table_service.list_tables()
    tables = [
        t for t in all_tables if org_table_ids is None or str(t.id) in org_table_ids
    ]

    org_robot_ids = await _org_scoped_ids(org_id, "device")
    all_robots = await robot_service.list_robots()
    robots = [
        r
        for r in all_robots
        if org_robot_ids is None or str(r.robot_id) in org_robot_ids
    ]

    # 测试运行可选"以哪个智能体身份执行"（LLM/表格节点需要智能体身份），仅同组织
    org_agent_ids = await _org_scoped_ids(org_id, "agent")
    all_agents = await agent_service.list_agents()
    agents = [
        a for a in all_agents if org_agent_ids is None or str(a.id) in org_agent_ids
    ]
    agent_opts = "".join(f'<option value="{a.id}">{esc(a.name)}</option>' for a in agents)

    # LLM 节点可挂载的技能（软行为指导）
    skills = await skill_service.list_skills()

    runs = await workflow_service.list_runs(workflow_id, limit=20)

    # 组织上下文透传：无查询串的 URL 用 oid_q，已有查询串的用 oid_amp
    oid_q = f"?org_id={org_id}" if org_id else ""
    oid_amp = f"&org_id={org_id}" if org_id else ""

    # 供 JS 动态添加 switch 分支行使用的选项（注意用单引号属性，避免破坏双引号 JS 字符串）
    node_names_js = [n["name"] for n in wf["nodes"]]
    cond_opts_js = "".join(
        f"<option value='{o}'>{l}</option>" for o, l in CONDITION_OPS
    )
    node_opts_js = "".join(
        f"<option value='{esc(nn)}'>{esc(nn)}</option>" for nn in node_names_js
    )

    # 节点表单/画布所需的节点名与类型清单
    node_names = [n["name"] for n in wf["nodes"]]
    node_info = [(n["name"], n["node_type"]) for n in wf["nodes"]]

    # 测试运行表单
    robot_opts = "".join(
        f'<option value="{esc(r.robot_id)}">{esc(r.device_name or r.robot_id)} ({esc(r.robot_id)})</option>'
        for r in robots
    )

    # 运行日志
    run_rows = "".join(
        f"<tr>"
        f"<td><code>#{r['id']}</code></td>"
        f"<td>{esc(r['robot_id']) or '—'}</td>"
        f"<td>{_status_badge(r['status'])}</td>"
        f"<td>{r['duration_ms']} ms</td>"
        f"<td>{_fmt_dt(r['created_at'])}</td>"
        f"<td><a href='/admin/workflow/{workflow_id}/run/{r['id']}{oid_q}' class='btn btn-ghost btn-sm'>详情</a></td>"
        f"</tr>"
        for r in runs
    )
    runs_html = run_rows or '<tr><td colspan="6" class="empty">暂无执行记录</td></tr>'

    # 输入参数声明（供智能体调用时明确传参；表单式编辑）
    try:
        schema_preview = wf.get("input_schema") or ""
        schema_items = json.loads(schema_preview) if schema_preview else []
    except Exception:
        schema_items = []

    def _schema_edit_row(s: dict) -> str:
        """一行输入参数编辑：字段名 / 类型 / 必填 / 说明 / 删除"""
        name = esc(str(s.get("name") or ""))
        desc = esc(str(s.get("description") or ""))
        typ = str(s.get("type") or "string")
        req = bool(s.get("required"))
        type_opts = "".join(
            f'<option value="{t}"{" selected" if t == typ else ""}>{l}</option>'
            for t, l in (("string", "文本"), ("number", "数字"), ("boolean", "布尔"))
        )
        req_opts = "".join(
            f'<option value="{v}"{" selected" if req == v else ""}>{l}</option>'
            for v, l in ((1, "必填"), (0, "可选"))
        )
        return (
            "<tr>"
            f'<td><input type="text" name="sc_name[]" value="{name}" placeholder="如 user_text" style="min-width:130px"></td>'
            f'<td><select name="sc_type[]" style="min-width:90px">{type_opts}</select></td>'
            f'<td><select name="sc_required[]" style="min-width:80px">{req_opts}</select></td>'
            f'<td><input type="text" name="sc_desc[]" value="{desc}" placeholder="这个参数给智能体的说明" style="min-width:200px"></td>'
            "<td><button type='button' class='btn btn-ghost btn-sm' onclick=\"this.closest('tr').remove()\">删除</button></td>"
            "</tr>"
        )

    schema_rows_html = "".join(
        _schema_edit_row(s) for s in schema_items if isinstance(s, dict)
    )
    schema_empty_hint = (
        '<p class="card-sub" style="margin:8px 0 0">'
        "未声明任何输入参数 —— 工作流不依赖用户消息，智能体调用时无需传参；"
        "<code>{{input.user_text}}</code> 无需声明即可在节点中引用（自动取当前消息文本，可留空）；"
        "系统自动注入的 robot_id / room_name 等字段同样无需声明（见下方清单）</p>"
        if not schema_rows_html
        else ""
    )

    # 系统自动注入字段说明（无需声明即可引用）
    sys_fields_html = """
    <div style="margin-top:14px;padding:12px 14px;background:#f8f9fb;border:1.5px dashed var(--border-strong);border-radius:var(--radius)">
        <div style="font-weight:600;margin-bottom:8px">⚙️ 系统自动注入字段（无需在下方声明，节点中直接 <code>{{input.字段名}}</code> 引用）</div>
        <table>
            <thead><tr><th>字段</th><th>含义</th><th>自动注入场景</th></tr></thead>
            <tbody>
                <tr><td><code>{{input.robot_id}}</code></td><td>当前设备 ID</td><td>全部场景（智能体会话 / 条件任务触发 / 测试运行）</td></tr>
                <tr><td><code>{{input.user_text}}</code></td><td>当前用户消息文本</td><td>智能体会话、测试运行（条件任务触发时为空）</td></tr>
                <tr><td><code>{{input.room_name}}</code></td><td>当前会话名</td><td>智能体会话、测试运行（条件任务触发时为空）</td></tr>
                <tr><td><code>{{input.room_type}}</code></td><td>会话类型：1 群聊 / 2 单聊</td><td>智能体会话、测试运行、消息类条件任务</td></tr>
                <tr><td><code>{{input.org_id}}</code></td><td>当前组织 ID</td><td>智能体会话</td></tr>
                <tr><td><code>{{input.sender}}</code></td><td>消息发送者</td><td>测试运行（填“测试用户”）；其他路径一般无值</td></tr>
                <tr><td><code>{{input.event_type}}</code> / <code>{{input.task_id}}</code> / <code>{{input.room}}</code></td><td>触发事件类型 / 触发任务 ID / 命中会话</td><td>条件任务触发工作流时</td></tr>
            </tbody>
        </table>
        <div style="font-size:12px;color:var(--text-3);margin-top:6px">提示：上述字段是引擎在执行工作流时自动带上的会话上下文；「输入参数声明」只用于声明智能体（LLM）调用时需要填写传递的业务参数。</div>
    </div>
    """

    schema_edit_html = f"""
    <div class="card">
        <h3>📥 输入参数声明（智能体调用时按此传参）</h3>
        <p class="card-sub">
            声明工作流需要的输入字段。智能体运行此工作流时按声明传参：
            <code>user_text</code> 自动取当前消息文本（无需声明即可用 <code>{{{{input.user_text}}}}</code>）；
            声明了其他字段则由智能体根据对话内容填写；不声明 <code>user_text</code> 表示工作流不依赖用户消息。
            系统内置字段（<code>robot_id</code> / <code>room_name</code> 等）由引擎自动注入、无需声明，见下方清单。
        </p>
        <form method="POST" action="/admin/workflow/{workflow_id}/input_schema{oid_q}">
            <div class="table-wrap">
            <table id="schemaTbl">
                <thead><tr><th>字段名 *（工作流中用 <code>{{{{input.字段名}}}}</code> 引用）</th><th>类型</th><th>必填</th><th>说明（给智能体看）</th><th></th></tr></thead>
                <tbody>{schema_rows_html}</tbody>
            </table>
            </div>
            <div style="display:flex;gap:10px;margin-top:10px">
                <button type="button" class="btn btn-ghost btn-sm" onclick="addSchemaRow(this)">+ 添加参数</button>
                <button type="submit" class="btn btn-primary btn-sm">保存声明</button>
            </div>
        </form>
        {schema_empty_hint}
        {sys_fields_html}
    </div>
    """

    content = f"""
    <link rel="stylesheet" href="/static/vendor/codemirror/codemirror.min.css">
    <script src="/static/vendor/codemirror/codemirror.min.js"></script>
    <script src="/static/vendor/codemirror/mode/python/python.min.js"></script>
    <style>.CodeMirror{{width:100%;border:1px solid var(--border-strong);border-radius:8px;height:auto;min-height:220px;font-size:13px}}</style>
    <div class="card">
        <div class="card-hd">
            <h3>🔄 {esc(wf['name'])} <span class="muted" style="font-size:13px">(#{workflow_id})</span></h3>
            <a href="/admin/workflows{oid_q}" class="btn btn-ghost btn-sm">← 返回列表</a>
        </div>
        <p class="card-sub">{esc(wf['description'])}｜超时: {wf['timeout_sec']}s</p>
        <div style="margin:6px 0">
            <form method="POST" action="/admin/workflow/{workflow_id}/sensitive{oid_q}" class="inline-form">
                <button class="btn btn-sm {'btn-warn' if wf.get('is_sensitive') else 'btn-ghost'}" style="color:#b45309"
                        title="涉及删除/导出/外部副作用等高风险操作时标记；授权给对话智能体时管理后台会警告（仅提示与审计，不强制拦截）">
                    {'⚠️ 已标记敏感（点击取消）' if wf.get('is_sensitive') else '⚠️ 标记为敏感工作流'}
                </button>
            </form>
        </div>
        <div class="note note-green">
            <b>变量引用</b>：输入消息 <code>{{{{input.user_text}}}}</code>；节点输出 <code>{{{{节点名.字段}}}}</code>，如查表节点 <code>{{{{q1.rows.0.order_no}}}}</code>、LLM 节点 <code>{{{{llm1.content}}}}</code>。<br>
            <b>输入契约</b>：在上方「📥 输入参数声明」中声明本工作流需要的输入字段（如 <code>user_text</code> 必填）。<code>{{{{input.user_text}}}}</code> 自动取当前用户消息；声明的其他字段由智能体调用时按对话内容填写。<br>
            <b>系统字段</b>：<code>{{{{input.robot_id}}}}</code>（当前设备）、<code>{{{{input.room_name}}}}</code>、<code>{{{{input.room_type}}}}</code> 等由系统自动注入、无需声明即可引用（完整清单见「📥 输入参数声明」卡片）。<br>
            <b>输出字段</b>：每个节点执行后都会产生输出，供后续节点用 <code>{{{{节点名.字段}}}}</code> 引用（如 LLM 输出 <code>content</code>、查表输出 <code>rows/count</code>、联网输出 <code>status_code/text</code>）。<br>
            <b>小技巧</b>：新建「输出节点」时，参数区会自动列出前面各节点可直接引用的字段与示例，照着抄即可。<br>
            <b>分支跳转</b>：每个节点可设置「成功跳转 / 失败跳转」并在下拉中直接选目标节点。<br>
            · 条件节点：满足 → 走「成功跳转」（空=顺序下个）；不满足 → 走「失败跳转」（空=中止）。<br>
            · <b>多分支节点（switch）</b>：按判断字段的值从上到下匹配分支，每个分支可设「大于/小于/等于/包含…」条件并指定跳转节点；<b>每个分支都要指定跳转目标</b>（留空 = 命中该分支就报错中止，可以选「（到此结束）」正常收尾），全不命中走「兜底跳转」；适合 >1、=0、<0 等多档判断。挂节点直接点画布上该分支线上的「＋」。<br>
            · <b>节点顺序</b>：顺序决定「空跳转」走向。普通节点用节点下方的「＋」在它下面插节点；<b>switch 的每个分支用它自己那条线上的「＋」挂新节点</b>（会写成显式跳转，多个分支互不干扰）。<br>
            · <b>代码节点</b>：运行 Python 代码处理数据，往 <code>output</code> 字典写结果（或直接赋值 <code>result</code>），异常走失败分支。<br>
            · 普通节点：执行失败 → 走「失败跳转」（空=中止）；异常同理。<br>
            · <b>跳回前面的节点 ≠ 循环</b>：每个节点整条工作流里最多执行一次。跳回一个已经跑过的节点，引擎会立刻判定「循环引用」并<b>中止整条工作流</b>（状态=循环，后面排着的节点也不会再跑）。所以想在某条线上收尾请用「（到此结束）」，别往回跳；重试/轮询这类真循环当前引擎不支持。<br>
            · 多个分支可以跳到同一个节点（多分支合并），这是允许的 —— 它只执行一次。
        </div>
    </div>
    {schema_edit_html}
    <div class="card">
        <h3>🧩 流程编排</h3>
        <p class="card-sub">点节点在右侧面板编辑；拖动空白处平移，滚轮缩放。绿线=成功、紫线=switch 分支、橙虚线=兜底、红线=失败。「结束」=流程到此收尾（分支配出去的节点默认就是它，所以两条分支的节点不会互相串起来）；「中止」=没设跳转目标，命中就报错停下；「⚠」=跳转目标已被删。<b>分支线上的「＋」= 给这条分支单独挂一个节点</b>，每条分支互不影响。</p>
        {_wf_canvas(wf, workflow_id, oid_q, oid_amp, tables, agents, skills)}
    </div>
    <div class="card">
        <h3>🧪 测试运行</h3>
        <form method="POST" action="/admin/workflow/{workflow_id}/test{oid_q}" class="form-row">
            <div class="form-group"><label>运行身份（选填）</label><select name="agent_id" style="min-width:160px"><option value="">（无，LLM/表格节点不可用）</option>{agent_opts}</select></div>
            <div class="form-group"><label>选择设备</label><select name="robot_id" style="min-width:200px"><option value="">（不指定，设备指令节点不可用）</option>{robot_opts}</select></div>
            <div class="form-group" style="flex:1"><label>模拟输入消息（选填）</label><input type="text" name="user_text" placeholder="留空=无用户消息输入（工作流可不依赖 user_text）"></div>
            <div class="form-group"><label>虚拟设备</label>
                <label class="form-check" style="margin-top:8px">
                    <input type="checkbox" name="simulate" value="1">
                    <span>勾选后设备指令节点返回模拟数据；未选择设备时自动使用模拟数据</span>
                </label>
            </div>
            <button class="btn btn-success">执行工作流</button>
        </form>
    </div>
    <div class="card">
        <h3>📜 执行日志（最近 20 条）</h3>
        <div class="table-wrap">
        <table>
            <thead><tr><th>运行ID</th><th>设备</th><th>状态</th><th>耗时</th><th>时间</th><th>操作</th></tr></thead>
            <tbody>{runs_html}</tbody>
        </table>
        </div>
    </div>
    <script>
    function showParams(sel){{
        var v=sel.value, form=sel.closest('form');
        form.querySelectorAll('.nparams').forEach(function(d){{
            var show = (d.getAttribute('data-ptype')===v);
            d.style.display = show ? 'flex' : 'none';
            // 隐藏的参数块控件必须禁用，否则其中的 required 输入会触发浏览器表单校验
            // （"invalid form control is not focusable"），导致表单无法提交
            d.querySelectorAll('input,select,textarea').forEach(function(c){{ c.disabled = !show; }});
            // 隐藏/显示块内的 CodeMirror 编辑器
            d.querySelectorAll('.CodeMirror').forEach(function(ce){{
                ce.style.display = show ? '' : 'none';
                if (show && ce.CodeMirror) ce.CodeMirror.refresh();
            }});
        }});
        // 跳转区：switch 用分支定义，其它类型用成功/失败跳转
        form.querySelectorAll('.njump').forEach(function(d){{
            var show = (d.getAttribute('data-jswitch') === '1') === (v === 'switch');
            d.style.display = show ? 'flex' : 'none';
            // 隐藏区里的控件必须禁用，否则它们照样会被提交，覆盖掉另一套跳转设置
            d.querySelectorAll('input,select,textarea').forEach(function(c){{ c.disabled = !show; }});
        }});
        // 设备指令参数表单：切换节点类型后，重新按已选指令类型应用字段组的显示/禁用状态
        var cs = form.querySelector('select[name="params[cmd_type]"]');
        if (cs && cs.__cmdParamUpd) cs.__cmdParamUpd();
    }}
    function initCodeEditors(){{
        if (!window.CodeMirror) return; // CDN 未加载则回退原生 textarea
        document.querySelectorAll('textarea[name="params[code]"]').forEach(function(ta){{
            if (ta._cmReady) return;
            ta._cmReady = true;
            // required 校验改由提交时自定义处理（CM 会隐藏原 textarea）
            ta.setAttribute('data-required', ta.hasAttribute('required') ? '1' : '');
            ta.removeAttribute('required');
            CodeMirror.fromTextArea(ta, {{
                mode: 'python',
                lineNumbers: true,
                indentUnit: 4,
                tabSize: 4,
                indentWithTabs: false,
                extraKeys: {{'Tab': function(cm){{ cm.replaceSelection('    ', 'end'); }} }}
            }}).on('change', function(){{ wfDirty = true; }});
        }});
    }}
    // 提交前：CodeMirror 内容写回原 textarea + 自定义必填校验
    document.querySelectorAll('form').forEach(function(form){{
        form.addEventListener('submit', function(e){{
            var blocked = false;
            form.querySelectorAll('.CodeMirror').forEach(function(ce){{
                if (ce.style.display === 'none') return;
                // 双保险：CodeMirror 自身未设置 display 时，按所属参数块判断是否隐藏
                var block = ce.closest ? ce.closest('.nparams') : null;
                if (block && block.style.display === 'none') return;
                var cm = ce.CodeMirror;
                if (!cm) return;
                var ta = cm.getTextArea();
                if (ta.getAttribute('data-required') === '1' && !cm.getValue().trim()) {{
                    e.preventDefault();
                    cm.focus();
                    blocked = true;
                    return;
                }}
                cm.save();
            }});
            if (blocked) alert('请填写 Python 代码');
        }});
    }});
    // 必须先初始化 CodeMirror 再调用 showParams：否则 showParams 查不到
    // 刚创建的 .CodeMirror 元素，无法把隐藏参数块内的编辑器设为 display:none，
    // 导致提交校验把隐藏的 code 编辑器误判为可见（LLM 等节点保存时报"请填写 Python 代码"）
    initCodeEditors();
    document.querySelectorAll('form select[name="node_type"]').forEach(function(sel){{ showParams(sel); }});
    function addSwitchRow(btn){{
        var tb=btn.closest('form').querySelector('#switchTbl tbody');
        var tr=document.createElement('tr');
        tr.innerHTML="<td><input type='text' name='branch_label[]' value='' placeholder='如 大于1'></td>"+
            "<td><select name='branch_op[]'>{cond_opts_js}</select></td>"+
            "<td><input type='text' name='branch_value[]' value='' placeholder='比较值'></td>"+
            "<td><select name='branch_next[]'><option value=''>（未指定 → 中止）</option>"+
            "<option value='{END_TARGET}'>（到此结束）</option>{node_opts_js}</select></td>"+
            "<td><button type='button' class='btn btn-ghost btn-sm' onclick=\\\"this.closest('tr').remove()\\\">删除</button></td>";
        tb.appendChild(tr);
    }}
    function addSchemaRow(btn){{
        var tb=btn.closest('form').querySelector('#schemaTbl tbody');
        var tr=document.createElement('tr');
        tr.innerHTML="<td><input type='text' name='sc_name[]' value='' placeholder='如 user_text'></td>"+
            "<td><select name='sc_type[]'><option value='string' selected>文本</option><option value='number'>数字</option><option value='boolean'>布尔</option></select></td>"+
            "<td><select name='sc_required[]'><option value='1'>必填</option><option value='0' selected>可选</option></select></td>"+
            "<td><input type='text' name='sc_desc[]' value='' placeholder='这个参数给智能体的说明'></td>"+
            "<td><button type='button' class='btn btn-ghost btn-sm' onclick=\\\"this.closest('tr').remove()\\\">删除</button></td>";
        tb.appendChild(tr);
    }}
    function addFieldRow(btn){{
        var body=btn.closest('form').querySelector('.tf-body');
        var row=document.createElement('div');
        row.className='tf-row';
        row.style.cssText='display:flex;gap:8px;align-items:center;margin-bottom:6px';
        row.innerHTML=
            "<input type='text' name='tf_name[]' value='' placeholder='字段名(字母开头)' style='flex:1;min-width:90px' required>"+
            "<select name='tf_type[]' style='width:110px'>"+
            "<option value='varchar'>varchar</option><option value='int'>int</option>"+
            "<option value='float'>float</option><option value='datetime'>datetime</option>"+
            "<option value='text'>text</option><option value='json'>json</option>"+
            "</select>"+
            "<input type='text' name='tf_label[]' value='' placeholder='中文名(可选)' style='flex:1;min-width:90px'>"+
            "<select name='tf_required[]' style='width:64px'>"+
            "<option value='0'>否</option><option value='1'>是</option>"+
            "</select>"+
            "<button type='button' class='btn btn-ghost btn-sm' onclick=\\\"this.closest('.tf-row').remove()\\\">删除</button>";
        body.appendChild(row);
    }}
    function addKVRow(btn){{
        var kind=btn.getAttribute('data-kind');
        var prefix=btn.getAttribute('data-pf');
        var body=btn.closest('.kv-editor').querySelector('.kv-body');
        var row=document.createElement('div');
        row.className='kv-row';
        row.style.cssText='display:flex;gap:8px;align-items:center;margin-bottom:6px';
        var html;
        if(kind==='cond'){{
            html="<input type='text' name='"+prefix+"_field[]' value='' placeholder='字段名' style='flex:1;min-width:90px' required>"+
                "<select name='"+prefix+"_op[]' style='width:80px'>"+
                "<option value='='>=</option><option value='!='>!=</option><option value='>'>></option>"+
                "<option value='<'><</option><option value='>='>>=</option><option value='<='><=</option>"+
                "<option value='like'>like</option></select>"+
                "<input type='text' name='"+prefix+"_value[]' value='' placeholder='比较值' style='flex:1;min-width:90px'>"+
                "<button type='button' class='btn btn-ghost btn-sm' onclick=\\\"this.closest('.kv-row').remove()\\\">删除</button>";
        }} else {{
            html="<input type='text' name='"+prefix+"_field[]' value='' placeholder='字段名/键' style='flex:1;min-width:90px' required>"+
                "<input type='text' name='"+prefix+"_value[]' value='' placeholder='值' style='flex:1;min-width:90px'>"+
                "<button type='button' class='btn btn-ghost btn-sm' onclick=\\\"this.closest('.kv-row').remove()\\\">删除</button>";
        }}
        row.innerHTML=html;
        body.appendChild(row);
    }}
    </script>
    """
    return page(
        f"编排 {wf['name']}",
        f"🔄 {esc(wf['name'])}",
        content,
        msg,
        active="/admin/workflows",
        org_id=org_id,
        org_name=cur_org_name,
        org_active="workflows",
    )


def _status_badge(status: str) -> str:
    color = {
        "success": "#16a34a",
        "failed": "#dc2626",
        "timeout": "#d97706",
        "loop": "#9333ea",
        "running": "#2563eb",
    }.get(status, "#64748b")
    label = {
        "success": "成功",
        "failed": "失败",
        "timeout": "超时",
        "loop": "循环",
        "running": "执行中",
    }.get(status, status)
    return f'<span style="color:#fff;background:{color};padding:2px 8px;border-radius:10px;font-size:12px">{label}</span>'


def _has_sim(obj) -> bool:
    """递归检测输出数据是否包含 simulated=True（虚拟设备返回）"""
    if isinstance(obj, dict):
        if obj.get("simulated"):
            return True
        return any(_has_sim(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_sim(v) for v in obj)
    return False


# ════════════════════════════════════════════
#  节点操作
# ════════════════════════════════════════════


@router.post("/admin/workflow/{workflow_id}/node/add")
async def admin_workflow_node_add(
    workflow_id: int, request: Request, org_id: int = Query(0)
):
    form = await request.form()
    try:
        params = _parse_params(form)
        # LLM 节点挂载技能（多选数组）
        skill_ids = [int(x) for x in form.getlist("params[skill_ids][]") if str(x).isdigit()]
        if skill_ids:
            params["skill_ids"] = skill_ids
        nt = form.get("node_type") or "llm"
        if nt == "switch":
            params["branches"] = _parse_switch_branches(form)
        elif nt == "table_create":
            params["fields"] = _parse_table_create_fields(form)
        elif nt == "table_query":
            params["where"] = _parse_cond_rows(form, "cq")
        elif nt == "table_insert":
            params["row"] = _parse_kv_fields(form, "ir", coerce=True)
        elif nt == "table_update":
            params["fields"] = _parse_kv_fields(form, "uf", coerce=True)
        elif nt == "http_get":
            params["headers"] = _parse_kv_fields(form, "hg")
        elif nt == "http_post":
            params["data"] = _parse_kv_fields(form, "hpd", coerce=True)
            params["headers"] = _parse_kv_fields(form, "hph")
        nxt = (form.get("next_on_success") or "").strip()
        # 「（自动接在最后）」：新节点先按"到此结束"落库，再把原链尾接过来。
        # 用户明确选了「（到此结束）」/某个节点时就不动上一个节点——不能偷偷改别人。
        auto = nxt in ("", AUTO_LINK)
        node_id = await workflow_service.add_node(
            workflow_id,
            WorkflowNodeRequest(
                node_type=form.get("node_type") or "llm",
                name=form.get("name") or "",
                params=params,
                next_on_success=END_TARGET if auto else nxt,
                next_on_fail=form.get("next_on_fail") or "",
            ),
            link_terminal_tail=auto,
        )
        base = (
            f"/admin/workflow/{workflow_id}?org_id={org_id}"
            if org_id
            else f"/admin/workflow/{workflow_id}"
        )
        return RedirectResponse(
            url=_with_msg(base, f"✅ 节点已添加 (#{node_id})"), status_code=303
        )
    except Exception as e:
        base = (
            f"/admin/workflow/{workflow_id}?org_id={org_id}"
            if org_id
            else f"/admin/workflow/{workflow_id}"
        )
        return RedirectResponse(url=_with_msg(base, f"❌ {esc(str(e))}"), status_code=303)


@router.post("/admin/workflow/{workflow_id}/node/{node_id}/update")
async def admin_workflow_node_update(
    workflow_id: int, node_id: int, request: Request, org_id: int = Query(0)
):
    form = await request.form()
    try:
        params = _parse_params(form)
        # LLM 节点挂载技能（多选数组）
        skill_ids = [int(x) for x in form.getlist("params[skill_ids][]") if str(x).isdigit()]
        if skill_ids:
            params["skill_ids"] = skill_ids
        nt = form.get("node_type") or "llm"
        if nt == "switch":
            params["branches"] = _parse_switch_branches(form)
        elif nt == "table_create":
            params["fields"] = _parse_table_create_fields(form)
        elif nt == "table_query":
            params["where"] = _parse_cond_rows(form, "cq")
        elif nt == "table_insert":
            params["row"] = _parse_kv_fields(form, "ir", coerce=True)
        elif nt == "table_update":
            params["fields"] = _parse_kv_fields(form, "uf", coerce=True)
        elif nt == "http_get":
            params["headers"] = _parse_kv_fields(form, "hg")
        elif nt == "http_post":
            params["data"] = _parse_kv_fields(form, "hpd", coerce=True)
            params["headers"] = _parse_kv_fields(form, "hph")
        ok = await workflow_service.update_node(
            node_id,
            WorkflowNodeRequest(
                node_type=form.get("node_type") or "llm",
                name=form.get("name") or "",
                params=params,
                next_on_success=form.get("next_on_success") or "",
                next_on_fail=form.get("next_on_fail") or "",
            ),
        )
        base = (
            f"/admin/workflow/{workflow_id}?org_id={org_id}"
            if org_id
            else f"/admin/workflow/{workflow_id}"
        )
        return RedirectResponse(
            url=_with_msg(base, "✅ 节点已更新" if ok else "❌ 节点不存在"),
            status_code=303,
        )
    except Exception as e:
        base = (
            f"/admin/workflow/{workflow_id}?org_id={org_id}"
            if org_id
            else f"/admin/workflow/{workflow_id}"
        )
        return RedirectResponse(url=_with_msg(base, f"❌ {esc(str(e))}"), status_code=303)


@router.get("/admin/workflow/{workflow_id}/node/{node_id}/insert")
async def admin_workflow_node_insert(
    workflow_id: int,
    node_id: int,
    org_id: int = Query(0),
    branch: Optional[int] = Query(None),
):
    """新建节点并挂到指定位置。

    - 不带 branch：插到 node_id 正下方，并接上原来的链条（源节点 → 新节点 → 原后续）
    - 带 branch：新节点**追加到末尾**、并把该分支的跳转目标指向它；兜底用 branch=-1。
      新节点默认带「到此结束」（END_TARGET）：分支配出去的节点是链尾，别自动流向
      后面新建的兄弟节点。
    """
    base = (
        f"/admin/workflow/{workflow_id}?org_id={org_id}"
        if org_id
        else f"/admin/workflow/{workflow_id}"
    )
    try:
        src = None
        if branch is not None:
            wf = await workflow_service.get_workflow(workflow_id) or {}
            src = next(
                (n for n in wf.get("nodes", []) if n["id"] == node_id),
                None,
            )
        if branch is None:
            new_id = await workflow_service.insert_node_after(
                workflow_id, node_id, WorkflowNodeRequest(node_type="llm", name="", params={})
            )
        else:
            new_id = await workflow_service.add_node(
                workflow_id,
                WorkflowNodeRequest(
                    node_type="llm",
                    name="",
                    params={},
                    next_on_success=END_TARGET,
                ),
            )
        if branch is not None and src:
            wf = await workflow_service.get_workflow(workflow_id) or {}
            new_name = next(
                (n["name"] for n in wf.get("nodes", []) if n["id"] == new_id), ""
            )
            params = dict(src.get("params") or {})
            branches = params.get("branches")
            ok = False
            if branch == -1:
                params["default_next"] = new_name
                ok = True
            elif isinstance(branches, list) and 0 <= branch < len(branches):
                if isinstance(branches[branch], dict):
                    branches[branch]["next"] = new_name
                    ok = True
            if ok:
                await workflow_service.update_node(
                    node_id,
                    WorkflowNodeRequest(
                        node_type=src["node_type"],
                        name=src["name"],
                        params=params,
                        next_on_success=src.get("next_on_success") or "",
                        next_on_fail=src.get("next_on_fail") or "",
                    ),
                )
    except Exception as e:
        return RedirectResponse(url=_with_msg(base, f"❌ {esc(str(e))}"), status_code=303)
    return RedirectResponse(
        url=_with_msg(base, f"✅ 节点已添加 (#{new_id})"), status_code=303
    )


@router.get("/admin/workflow/{workflow_id}/node/{node_id}/delete")
async def admin_workflow_node_delete(
    workflow_id: int, node_id: int, org_id: int = Query(0)
):
    base = (
        f"/admin/workflow/{workflow_id}?org_id={org_id}"
        if org_id
        else f"/admin/workflow/{workflow_id}"
    )
    try:
        ok = await workflow_service.delete_node(node_id)
    except Exception as e:
        return RedirectResponse(url=_with_msg(base, f"❌ {esc(str(e))}"), status_code=303)
    return RedirectResponse(
        url=_with_msg(base, "✅ 已删除" if ok else "❌ 节点不存在"), status_code=303
    )


def _parse_table_create_fields(form) -> list:
    """解析创建表格节点字段表单：tf_name/tf_type/tf_label/tf_required 四个数组 → [{name,type,label,required}]"""
    names = form.getlist("tf_name[]")
    types = form.getlist("tf_type[]")
    labels = form.getlist("tf_label[]")
    reqs = form.getlist("tf_required[]")
    fields = []
    for i, name in enumerate(names):
        name = (name or "").strip()
        if not name:
            continue
        fields.append(
            {
                "name": name,
                "type": (types[i] if i < len(types) else "varchar") or "varchar",
                "label": (labels[i] if i < len(labels) else "").strip(),
                "required": (reqs[i] if i < len(reqs) else "0") == "1",
            }
        )
    return fields


def _coerce_val(v):
    """表单字符串值轻量转换：数字→int/float，true/false→bool，其余保持字符串"""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if not s:
        return ""
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return v


def _parse_kv_fields(form, prefix, coerce=False) -> dict:
    """解析 键/值 行表单 → {字段名: 值}；空键行跳过；coerce=True 时数值/布尔自动转换"""
    fields = form.getlist(f"{prefix}_field[]")
    values = form.getlist(f"{prefix}_value[]")
    out = {}
    for i, f in enumerate(fields):
        f = (f or "").strip()
        if not f:
            continue
        v = values[i] if i < len(values) else ""
        out[f] = _coerce_val(v) if coerce else v
    return out


def _parse_cond_rows(form, prefix) -> list:
    """解析 条件行表单 → [{field, op, value}]；空字段名行跳过"""
    fields = form.getlist(f"{prefix}_field[]")
    ops = form.getlist(f"{prefix}_op[]")
    values = form.getlist(f"{prefix}_value[]")
    rows = []
    for i, f in enumerate(fields):
        f = (f or "").strip()
        if not f:
            continue
        rows.append(
            {
                "field": f,
                "op": (ops[i] if i < len(ops) else "=") or "=",
                "value": values[i] if i < len(values) else "",
            }
        )
    return rows


def _parse_switch_branches(form) -> list:
    """解析多分支节点表格：branch_label/op/value/next 四个数组字段"""
    labels = form.getlist("branch_label[]")
    ops = form.getlist("branch_op[]")
    values = form.getlist("branch_value[]")
    nexts = form.getlist("branch_next[]")
    branches = []
    for i, label in enumerate(labels):
        if not (label or "").strip():
            continue
        branches.append(
            {
                "label": (label or "").strip(),
                "op": ops[i] if i < len(ops) else "==",
                "value": values[i] if i < len(values) else "",
                "next": nexts[i] if i < len(nexts) else "",
            }
        )
    return branches


def _parse_params(form) -> dict:
    """从表单 params[key] 字段解析参数（JSON 字段自动解析）"""
    params = {}
    prefix = "params["
    for key in form.multi_items():
        k = key[0]
        if not k.startswith(prefix) or not k.endswith("]"):
            continue
        fname = k[len(prefix) : -1]
        raw = key[1]
        # params[name][] 多值字段（checkbox 组 / 成对行）→ 收集成列表。
        # 不做这一步，fname 会变成 "name][" 这种垃圾键，且值只剩最后一个。
        if fname.endswith("]["):
            params.setdefault(fname[:-2], []).append(raw)
            continue
        if "[" in fname or "]" in fname:
            continue
        params[fname] = raw
    # JSON 字段解析
    for jf in ("where", "row", "fields", "data", "headers", "params"):
        if jf in params and isinstance(params[jf], str) and params[jf].strip():
            v = params[jf].strip()
            if v.startswith(("{", "[")):
                try:
                    params[jf] = json.loads(v)
                except Exception:
                    pass  # 保留原字符串
    # 数值字段
    for nf in ("limit", "id", "wait_sec", "agent_id"):
        if nf in params and params[nf] not in ("", None):
            try:
                params[nf] = int(params[nf])
            except ValueError:
                pass
    # 设备指令参数表单：params[p_字段名] → params.params 字典（跳过空值）
    extra = {}
    for k in list(params.keys()):
        if k.startswith("p_"):
            v = params.pop(k)
            if isinstance(v, str):
                v = v.strip()
            if v not in ("", None):
                extra[k[2:]] = v
    if extra:
        params["params"] = extra
    return params


# ════════════════════════════════════════════
#  测试运行 & 日志详情
# ════════════════════════════════════════════


@router.post("/admin/workflow/{workflow_id}/test")
async def admin_workflow_test(
    workflow_id: int,
    robot_id: str = Form(""),
    user_text: str = Form(""),
    simulate: bool = Form(False),
    agent_id: int = Form(0),
    org_id: int = Query(0),
):
    try:
        # 未选择设备时自动降级为模拟模式（否则设备指令节点无法执行）
        simulate = bool(simulate) or not robot_id
        agent = await agent_service.get_agent(agent_id) if agent_id else None
        input_ctx = {
            "robot_id": robot_id,
            "room_name": "测试",
            "room_type": 1,
            "user_text": user_text,
            "sender": "测试用户",
            "timestamp": __import__("datetime").datetime.now().isoformat(),
        }
        result = await workflow_engine.execute(
            workflow_id, robot_id, input_ctx, simulate=simulate, agent=agent
        )
        run_id = result.get("run_id")
        if result.get("status") == "success":
            msg = f"✅ 执行成功（{result['duration_ms']}ms）"
        else:
            msg = f"❌ {result.get('status')}: {result.get('error') or '执行失败'}"
        base = (
            f"/admin/workflow/{workflow_id}/run/{run_id}?org_id={org_id}"
            if org_id
            else f"/admin/workflow/{workflow_id}/run/{run_id}"
        )
        return RedirectResponse(url=_with_msg(base, msg), status_code=303)
    except Exception as e:
        logger.error(f"workflow test failed: {e}")
        base = (
            f"/admin/workflow/{workflow_id}?org_id={org_id}"
            if org_id
            else f"/admin/workflow/{workflow_id}"
        )
        return RedirectResponse(url=_with_msg(base, f"❌ {esc(str(e))}"), status_code=303)


@router.get("/admin/workflow/{workflow_id}/run/{run_id}")
async def admin_workflow_run_detail(
    workflow_id: int, run_id: int, msg: str = "", org_id: int = Query(0)
):
    run = await workflow_service.get_run(run_id)
    if not run:
        base = (
            f"/admin/workflow/{workflow_id}?org_id={org_id}"
            if org_id
            else f"/admin/workflow/{workflow_id}"
        )
        return RedirectResponse(url=_with_msg(base, "❌ 运行记录不存在"), status_code=303)
    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if grp:
            cur_org_name = grp["name"]
    wf = await workflow_service.get_workflow(workflow_id)
    wf_name = wf["name"] if wf else f"#{workflow_id}"
    oid_q = f"?org_id={org_id}" if org_id else ""

    node_rows = ""
    for n in run.get("nodes", []):
        sim_tag = ""
        if _has_sim(n.get("output")):
            sim_tag = ' <span class="badge badge-blue">模拟数据</span>'
        node_rows += (
            f"<tr>"
            f"<td><code>{esc(n['node_name'])}</code></td>"
            f"<td>{esc(n['node_type'])}{sim_tag}</td>"
            f"<td>{_status_badge(n['status'])}</td>"
            f"<td>{n['duration_ms']} ms</td>"
            f"<td class='mono' style='max-width:260px;font-size:12px'>{esc(json.dumps(n['input'], ensure_ascii=False))}</td>"
            f"<td class='mono' style='max-width:260px;font-size:12px'>{esc(json.dumps(n['output'], ensure_ascii=False))}</td>"
            f"<td style='color:#dc2626;font-size:12px'>{esc(n['error'] or '')}</td>"
            f"</tr>"
        )
    nodes_html = node_rows or '<tr><td colspan="7" class="empty">无节点日志</td></tr>'

    content = f"""
    <div class="card">
        <div class="card-hd">
            <h3>📜 运行日志 #{run_id} <span class="muted" style="font-size:13px">{esc(wf_name)}</span></h3>
            <a href="/admin/workflow/{workflow_id}{oid_q}" class="btn btn-ghost btn-sm">← 返回编排</a>
        </div>
        <div class="stat-grid" style="margin-top:4px">
            <div class="stat-card"><div class="stat-label">状态</div><div style="margin-top:6px">{_status_badge(run['status'])}</div></div>
            <div class="stat-card"><div class="stat-label">设备</div><div class="stat-num" style="font-size:20px">{esc(run['robot_id']) or '—'}</div></div>
            <div class="stat-card"><div class="stat-label">耗时</div><div class="stat-num" style="font-size:20px">{run['duration_ms']} ms</div></div>
            <div class="stat-card"><div class="stat-label">时间</div><div class="stat-num" style="font-size:20px">{_fmt_dt(run['created_at'])}</div></div>
        </div>
        <p class="card-sub" style="margin-top:14px"><b>最终输出</b>：<code>{esc(str(run.get('output') or ''))}</code></p>
        <p class="card-sub" style="color:#dc2626">{esc(run.get('error') or '')}</p>
    </div>
    <div class="card">
        <h3>🧩 节点执行明细</h3>
        <div class="table-wrap">
        <table>
            <thead><tr><th>节点</th><th>类型</th><th>状态</th><th>耗时</th><th>输入</th><th>输出</th><th>错误</th></tr></thead>
            <tbody>{nodes_html}</tbody>
        </table>
        </div>
    </div>
    """
    return page(
        f"运行日志 #{run_id}",
        f"📜 {esc(wf_name)}",
        content,
        msg,
        active="/admin/workflows",
        org_id=org_id,
        org_name=cur_org_name,
        org_active="workflows",
    )
