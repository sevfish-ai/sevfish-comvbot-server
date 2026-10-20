"""
Test page — all Aivbot features covered.
"""

import json as json_mod
import logging
import os
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from ..services import robot_service, aivbot_service
from ..layout import page, esc

logger = logging.getLogger(__name__)
router = APIRouter(tags=["test"])


def _is_online(robot_id: str) -> bool:
    """设备是否在线：以 WebSocket 连接注册表为准（robots.status 那列会漂移）。"""
    return robot_id in aivbot_service.active_connections


def make_select(robots, name="client_id", sel_id=""):
    """simple device dropdown"""
    opts = []
    for r in robots:
        sel = " selected" if _is_online(r.robot_id) else ""
        opts.append(
            f'<option value="{esc(r.robot_id)}"{sel}>{esc(r.robot_id)} ({esc(r.device_name)})</option>'
        )
    if not opts:
        opts.append('<option value="">无设备</option>')
    attrs = f'name="{name}"'
    if sel_id:
        attrs += f' id="{sel_id}"'
    return f'<select {attrs} style="width:auto;min-width:170px">{"".join(opts)}</select>'


def _device_hidden(cid=""):
    """hidden client_id field, synced from the global device selector via JS"""
    return (
        f'<input type="hidden" name="client_id" class="js-client-id" '
        f'value="{esc(cid)}">'
    )


def _input(name, placeholder="", value="", size=None, required=False):
    s = f'<input type="text" name="{name}"'
    if placeholder:
        s += f' placeholder="{placeholder}"'
    if value:
        s += f' value="{value}"'
    if size:
        s += f' style="width:{size}px"'
    else:
        s += ' style="width:auto"'
    if required:
        s += " required"
    return s + ">"


def _cmd_form(cmd_type, *fields, device_sel="", label="", btn_cls=""):
    """Build a form for a parameterized command."""
    btn = f'<button class="btn {btn_cls}">{label}</button>' if label else ""
    parts = [
        f'<form method="POST" action="/test/cmd_extra" class="form-row">',
        device_sel,
        f'<input type="hidden" name="cmd_type" value="{cmd_type}">',
    ]
    for name, placeholder, *rest in fields:
        val = rest[0] if rest else ""
        required = rest[1] if len(rest) > 1 else False
        parts.append(_input(name, placeholder, value=val, required=required))
    parts.append(btn)
    parts.append("</form>")
    return "".join(parts)


@router.get("/test")
async def test_page(msg: str = ""):
    robots = await robot_service.list_robots()

    parts = []

    # status message
    if msg == "sent":
        parts.append('<div class="msg msg-success">✅ 命令已发送</div>')
    elif msg == "no_device":
        parts.append('<div class="msg msg-error">❌ 请选择设备</div>')
    elif msg == "no_robot":
        parts.append('<div class="msg msg-error">❌ 设备不在线</div>')
    elif msg.startswith("err:"):
        parts.append(f'<div class="msg msg-error">❌ {esc(msg[4:])}</div>')

    default_cid = next(
        (r.robot_id for r in robots if _is_online(r.robot_id)),
        robots[0].robot_id if robots else "",
    )
    device_sel = make_select(robots, sel_id="test-device")
    conn_count = len(aivbot_service.active_connections)

    # ── 测试设备（全局选择一次，所有测试共用）──
    parts.append('<div class="card" style="padding:14px 20px">')
    parts.append(
        f'<div class="form-row" style="align-items:center;gap:10px;margin:0">'
        f'<span class="field-label" style="margin:0;white-space:nowrap">🎯 测试设备</span>'
        f"{device_sel}"
        f'<span class="muted" style="font-size:12px">所有测试命令均发送到该设备</span>'
        f"</div>"
    )
    parts.append("</div>")

    # ── Device list ──
    parts.append('<div class="card">')
    parts.append(f"<h3>📱 在线设备（已连接 {conn_count}）</h3>")
    parts.append(
        "<table><tr><th>ID</th><th>设备名</th><th>状态</th><th>最后活跃</th></tr>"
    )
    for r in robots:
        if _is_online(r.robot_id):
            parts.append(
                f'<tr><td><code>{esc(r.robot_id)}</code></td><td>{esc(r.device_name)}</td><td><span class="badge badge-online">在线</span></td><td>{r.last_seen or "-"}</td></tr>'
            )
        else:
            parts.append(
                f'<tr><td><code>{esc(r.robot_id)}</code></td><td>{esc(r.device_name)}</td><td><span class="badge badge-offline">离线</span></td><td>{r.last_seen or "-"}</td></tr>'
            )
    if not robots:
        parts.append('<tr><td colspan="4" class="empty">暂无注册设备</td></tr>')
    parts.append("</table>")
    parts.append("</div>")

    # ── 一键命令 ──
    parts.append('<div class="card">')
    parts.append("<h3>🚀 一键命令</h3>")
    parts.append('<div class="form-row" style="gap:8px">')
    for label, cmd_type, c in [
        ("📇 获取全部好友", "507", "btn-ghost"),
        ("👥 获取全部群详情", "508", "btn-success"),
        ("💬 获取最近聊天", "505", "btn-ghost"),
        ("👤 获取我的信息", "503", "btn-ghost"),
        ("🏢 获取企业列表", "506", "btn-ghost"),
        ("🔴 开始接收消息", "202", "btn-danger"),
        ("⏹ 停止任务回首页", "201", "btn-ghost"),
        ("✅ 通过所有好友请求", "212", "btn-success"),
        ("⏰ 打卡", "222", "btn-ghost"),
        ("📤 群发助手（不可用）", "510", "btn-ghost"),
        ("📝 自动发表朋友圈（不可用）", "511", "btn-ghost"),
    ]:
        parts.append(
            f'<form method="POST" action="/test/cmd" style="display:inline-flex;margin:0">'
            f"{_device_hidden(default_cid)}"
            f'<input type="hidden" name="cmd_type" value="{cmd_type}">'
            f'<button class="btn {c} btn-sm">{label}</button>'
            f"</form>"
        )
    parts.append("</div>")
    parts.append("</div>")

    # ── 消息操作 ──
    parts.append('<div class="card">')
    parts.append("<h3>✉️ 消息操作</h3>")

    # 203 发送消息
    parts.append("<h3>发送消息 (203)</h3>")
    parts.append(
        f'<form method="POST" action="/test/send" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'{_input("to_name", "发给谁", "文件传输助手", size=170)}'
        f'{_input("text", "消息内容", "测试消息", size=210)}'
        f'<button class="btn btn-primary btn-sm">发送</button>'
        f"</form>"
    )

    # 204 指定回复
    parts.append("<h3>指定回复消息 (204)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="204">'
        f'{_input("titleList", "房间名(逗号分隔)", "文件传输助手", size=170)}'
        f'{_input("receivedName", "原始发送者", size=130)}'
        f'{_input("originalContent", "原始消息内容", size=130)}'
        f'{_input("textType", "消息类型(1文本)", "1", size=90)}'
        f'{_input("receivedContent", "回复内容", "回复消息", size=170)}'
        f'<button class="btn btn-primary btn-sm">回复</button>'
        f"</form>"
    )

    # 226 撤回消息
    parts.append("<h3>撤回消息 (226)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="226">'
        f'{_input("titleList", "房间名", "文件传输助手")}'
        f'{_input("originalContent", "要撤回的消息内容")}'
        f'{_input("textType", "消息类型(1文本)", "1")}'
        f'<button class="btn btn-danger btn-sm">撤回</button>'
        f"</form>"
    )

    # 205 转发消息
    parts.append("<h3>转发消息 (205)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="205">'
        f'{_input("titleList", "源房间名", "文件传输助手")}'
        f'{_input("receivedName", "原始发送者")}'
        f'{_input("originalContent", "原始消息内容")}'
        f'{_input("textType", "消息类型(1文本)", "1")}'
        f'{_input("nameList", "转发到(逗号分隔)")}'
        f'{_input("extraText", "附加留言")}'
        f'<button class="btn btn-primary btn-sm">转发</button>'
        f"</form>"
    )
    parts.append("</div>")

    # ── 群管理 ──
    parts.append('<div class="card">')
    parts.append("<h3>👥 群管理</h3>")

    # 206 创建群
    parts.append("<h3>创建群 (206)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="206">'
        f'{_input("groupName", "群名", "测试群", required=True)}'
        f'{_input("selectList", "成员(逗号分隔)", required=True)}'
        f'{_input("groupAnnouncement", "群公告")}'
        f'{_input("groupRemark", "群备注")}'
        f'{_input("groupTemplate", "群模板ID")}'
        f'<button class="btn btn-success btn-sm">创建群</button>'
        f"</form>"
    )

    # 207 修改群配置
    parts.append("<h3>修改群配置 (207)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="207">'
        f'{_input("groupName", "待修改群名", required=True)}'
        f'{_input("newGroupName", "新群名")}'
        f'{_input("newGroupAnnouncement", "新群公告")}'
        f'{_input("selectList", "拉人(逗号分隔)")}'
        f'{_input("removeList", "踢人(逗号分隔)")}'
        f'{_input("groupRemark", "新群备注")}'
        f'{_input("groupTemplate", "群模板ID")}'
        f'<button class="btn btn-ghost btn-sm">修改</button>'
        f"</form>"
    )

    # 219 解散群
    parts.append("<h3>解散群 (219)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="219">'
        f'{_input("groupName", "待解散群名", required=True)}'
        f'<button class="btn btn-danger btn-sm">解散群</button>'
        f"</form>"
    )

    # 214 展示群信息
    parts.append("<h3>展示群信息 (214)（不可用，与501重复）</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="214">'
        f'{_input("titleList", "群名", required=True)}'
        f'<button class="btn btn-primary btn-sm">展示</button>'
        f"</form>"
    )

    # 225 修改群成员信息
    parts.append("<h3>修改群成员信息 (225)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="225">'
        f'{_input("groupName", "群名", required=True)}'
        f'{_input("friend_name", "成员昵称", required=True)}'
        f'{_input("friend_markName", "新备注名")}'
        f'<button class="btn btn-ghost btn-sm">修改</button>'
        f"</form>"
    )

    # 501 获取群信息（详情）
    parts.append("<h3>获取群详情 (501)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="501">'
        f'{_input("selectList", "群名", required=True)}'
        f'<button class="btn btn-primary btn-sm">获取详情</button>'
        f"</form>"
    )
    parts.append("</div>")

    # ── 好友/联系人 ──
    parts.append('<div class="card">')
    parts.append("<h3>👤 好友/联系人</h3>")

    # 502 获取好友资料
    parts.append("<h3>获取好友资料 (502)</h3>")
    parts.append(
        f'<form method="POST" action="/test/friend_info" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'{_input("friend_name", "好友昵称", required=True)}'
        f'<button class="btn btn-primary btn-sm">获取资料</button>'
        f"</form>"
    )

    # 213 修改好友备注
    parts.append("<h3>修改好友备注 (213)</h3>")
    parts.append(
        f'<form method="POST" action="/test/remark" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'{_input("friend_name", "好友昵称", required=True)}'
        f'{_input("mark_name", "新备注名", required=True)}'
        f'<button class="btn btn-ghost btn-sm">修改备注</button>'
        f"</form>"
    )

    # 213 按手机号添加好友
    parts.append("<h3>按手机号添加好友 (213)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="213">'
        f'{_input("friend_phone", "手机号", required=True)}'
        f'{_input("friend_name", "好友姓名(选填)")}'
        f'{_input("friend_markName", "备注名")}'
        f'{_input("friend_markCorp", "备注企业")}'
        f'{_input("friend_markExtra", "备注描述")}'
        f'{_input("friend_leavingMsg", "好友验证附言")}'
        f'<button class="btn btn-success btn-sm">添加好友</button>'
        f"</form>"
    )

    # 220 从外部群添加好友
    parts.append("<h3>从外部群添加好友 (220)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="220">'
        f'{_input("groupName", "外部群名", required=True)}'
        f'{_input("friend_name", "好友昵称", required=True)}'
        f'<button class="btn btn-success btn-sm">添加好友</button>'
        f"</form>"
    )

    # 234 删除联系人
    parts.append("<h3>删除联系人 (234)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="234">'
        f'{_input("friend_name", "联系人昵称", required=True)}'
        f'<button class="btn btn-danger btn-sm">删除</button>'
        f"</form>"
    )
    parts.append("</div>")

    # ── 内容推送 ──
    parts.append('<div class="card">')
    parts.append("<h3>📎 内容推送</h3>")

    # 218 推送文件
    parts.append("<h3>推送文件 (218)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row" id="form218">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="218">'
        f'{_input("titleList", "发送给(逗号分隔)", required=True)}'
        f'<input type="text" id="objectName_218" name="objectName" placeholder="文件名" value="test.jpg">'
        f'<input type="text" id="fileUrl_218" name="fileUrl" placeholder="文件URL(在线文件)">'
        f'<input type="text" id="fileType_218" name="fileType" placeholder="文件类型" value="jpg">'
        f'{_input("extraText", "附加留言")}'
        f'<input type="hidden" id="fileBase64_218" name="fileBase64">'
        f'<input type="file" id="filePick_218" style="font-size:13px;max-width:260px">'
        f'<span id="fileInfo_218" style="font-size:11px;color:#94a3b8">本地文件→Base64，建议&lt;1MB</span>'
        f'<button class="btn btn-primary btn-sm">推送文件</button>'
        f"</form>"
    )

    # 224 推送链接
    parts.append("<h3>推送链接 (224)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="224">'
        f'{_input("titleList", "发送给(逗号分隔)", required=True)}'
        f'{_input("objectName", "文章标题")}'
        f'{_input("receivedContent", "文章副标题")}'
        f'{_input("originalContent", "链接地址", "https://")}'
        f'{_input("fileUrl", "封面图片URL")}'
        f'{_input("extraText", "附加留言")}'
        f'<button class="btn btn-primary btn-sm">推送链接</button>'
        f"</form>"
    )

    # 208 推送微盘图片
    parts.append("<h3>推送微盘图片 (208)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="208">'
        f'{_input("titleList", "发送给(逗号分隔)", required=True)}'
        f'{_input("objectName", "图片名称")}'
        f'{_input("extraText", "附加留言")}'
        f'<button class="btn btn-primary btn-sm">推送</button>'
        f"</form>"
    )

    # 209 推送微盘文件
    parts.append("<h3>推送微盘文件 (209)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="209">'
        f'{_input("titleList", "发送给(逗号分隔)", required=True)}'
        f'{_input("objectName", "文件名称")}'
        f'{_input("extraText", "附加留言")}'
        f'<button class="btn btn-primary btn-sm">推送</button>'
        f"</form>"
    )

    # 210 推送小程序
    parts.append("<h3>推送小程序 (210)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="210">'
        f'{_input("titleList", "发送给(逗号分隔)", required=True)}'
        f'{_input("objectName", "小程序名称")}'
        f'{_input("receivedContent", "小程序描述")}'
        f'{_input("originalContent", "小程序链接")}'
        f'{_input("fileUrl", "封面图片URL")}'
        f'{_input("extraText", "附加留言")}'
        f'<button class="btn btn-primary btn-sm">推送</button>'
        f"</form>"
    )

    # 211 推送腾讯文档
    parts.append("<h3>推送腾讯文档 (211)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="211">'
        f'{_input("titleList", "发送给(逗号分隔)", required=True)}'
        f'{_input("objectName", "文档名称")}'
        f'{_input("extraText", "附加留言")}'
        f'<button class="btn btn-primary btn-sm">推送</button>'
        f"</form>"
    )
    parts.append("</div>")

    # ── 其他操作 ──
    parts.append('<div class="card">')
    parts.append("<h3>⚙️ 其他操作</h3>")

    # 221 添加待办
    parts.append("<h3>添加待办 (221)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="221">'
        f'{_input("titleList", "成员(逗号分隔,选填)")}'
        f'{_input("receivedContent", "待办内容", required=True)}'
        f'{_input("description", "描述(选填)")}'
        f'<button class="btn btn-ghost btn-sm">添加待办</button>'
        f"</form>"
    )

    # 223 切换企业
    parts.append("<h3>切换企业 (223)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="223">'
        f'{_input("objectName", "企业名称", required=True)}'
        f'<button class="btn btn-primary btn-sm">切换</button>'
        f"</form>"
    )

    # 233 扫一扫
    parts.append("<h3>扫一扫 (233)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="233">'
        f'{_input("fileUrl", "二维码图片URL")}'
        f'<button class="btn btn-primary btn-sm">扫码</button>'
        f"</form>"
    )

    # 509 获取本地文件
    parts.append("<h3>获取本地文件 (509)</h3>")
    parts.append(
        f'<form method="POST" action="/test/cmd_extra" class="form-row">'
        f"{_device_hidden(default_cid)}"
        f'<input type="hidden" name="cmd_type" value="509">'
        f'{_input("fileUrl", "设备文件路径", required=True)}'
        f'<button class="btn btn-primary btn-sm">获取</button>'
        f"</form>"
    )
    parts.append("</div>")

    # ── 设备返回数据（右侧固定栏，与左侧测试功能左右排列）──
    data_parts = ['<div class="card">', "<h3>📦 设备返回数据</h3>"]
    has_data = False
    for r in robots:
        if not _is_online(r.robot_id):
            continue
        cid = r.robot_id
        data = aivbot_service.get_received_data(cid)
        # 兼容历史脚本留下的共享数据文件（websocket_server.py 已删除）
        shared_file = os.path.join(
            os.path.dirname(__file__), "..", "..", "shared_device_data.json"
        )
        if os.path.exists(shared_file):
            try:
                with open(shared_file, "r", encoding="utf-8") as f:
                    shared_data = json_mod.load(f)
                for scid, sdata in shared_data.items():
                    if scid == cid:
                        for key, items in sdata.items():
                            if key not in data:
                                data[key] = []
                            # 只追加不重复的条目
                            existing = json_mod.dumps(
                                data[key], ensure_ascii=False, default=str
                            )
                            for item in items:
                                if (
                                    json_mod.dumps(
                                        item, ensure_ascii=False, default=str
                                    )
                                    not in existing
                                ):
                                    data[key].append(item)
            except Exception:
                pass
        if not data:
            continue
        has_data = True
        data_parts.append(f"<h3>{esc(cid)} ({esc(r.device_name)})</h3>")
        label_map = {
            "last_result": "执行结果",
            "contacts": "通讯录（全部好友）",
            "groups": "群列表",
            "recent_list": "最近聊天",
            "group_info_detail": "群详情",
            "my_info": "我的信息",
            "corp_list": "企业列表",
            "friend_info": "好友资料",
            "recent_messages": "接收到的消息",
            "device_log": "设备错误日志",
        }
        for key, items in data.items():
            title = label_map.get(key, key)
            if not items:
                continue
            data_parts.append(
                f'<div class="inline-flex" style="gap:6px;margin:8px 0 2px"><strong>{title}</strong> <span class="badge badge-offline">{len(items)}条</span></div>'
            )

            if key == "recent_messages":
                for it in reversed(items[-20:]):
                    data_parts.append(
                        f"<pre>{esc(json_mod.dumps(it.get('raw', it), ensure_ascii=False, indent=2))}</pre>"
                    )
            elif key == "group_info_detail":
                # 群详情：展示全部群（每个群一个折叠块，按群名去重后的顺序）
                for it in items:
                    gname = it.get("group_name") or "(未命名群)"
                    owner = it.get("owner") or "-"
                    mcount = it.get("member_count", "-")
                    members = it.get("members") or []
                    summary = (
                        f'<summary style="cursor:pointer;user-select:none">'
                        f"👥 {esc(gname)} &nbsp;·&nbsp; 群主: {esc(owner)} &nbsp;·&nbsp; 成员: {mcount}人"
                        f"</summary>"
                    )
                    body = ""
                    if members:
                        body += f'<div style="margin:4px 0">成员: {esc("、".join(members))}</div>'
                    if it.get("announcement"):
                        body += f'<div style="margin:4px 0">公告: {esc(it.get("announcement"))}</div>'
                    if it.get("remark"):
                        body += f'<div style="margin:4px 0">备注: {esc(it.get("remark"))}</div>'
                    body += (
                        f"<pre style=\"margin:4px 0 0\">{esc(json_mod.dumps(it.get('raw', it), ensure_ascii=False, indent=2))}</pre>"
                    )
                    data_parts.append(f"<details style=\"margin:6px 0\">{summary}{body}</details>")
            else:
                last = items[-1]
                data_parts.append(
                    f"<pre>{esc(json_mod.dumps(last, ensure_ascii=False, indent=2))}</pre>"
                )
    if not has_data:
        data_parts.append(
            '<p class="muted">暂无返回数据，点击左侧按钮后结果会显示在这里</p>'
        )
    data_parts.append("</div>")

    # 全局测试设备 → 所有表单的 hidden client_id 同步（记忆上次选择）
    device_script = (
        "<script>"
        "(function(){"
        "var sel=document.getElementById('test-device');"
        "if(!sel)return;"
        "var KEY='dsh.testDevice';"
        "try{"
        "var saved=localStorage.getItem(KEY);"
        "if(saved){"
        "for(var i=0;i<sel.options.length;i++){"
        "if(sel.options[i].value===saved){sel.selectedIndex=i;break;}"
        "}"
        "}"
        "}catch(e){}"
        "function sync(){"
        "var v=sel.value;"
        "var h=document.querySelectorAll('input.js-client-id');"
        "for(var i=0;i<h.length;i++){h[i].value=v;}"
        "try{localStorage.setItem(KEY,v);}catch(e){}"
        "}"
        "sel.addEventListener('change',sync);"
        "sync();"
        "})();"
        "</script>"
    )

    script_html = (
        "<script>"
        "(function(){"
        "var pick=document.getElementById('filePick_218');"
        "if(!pick)return;"
        "var b64=document.getElementById('fileBase64_218');"
        "var info=document.getElementById('fileInfo_218');"
        "var obj=document.getElementById('objectName_218');"
        "var ftype=document.getElementById('fileType_218');"
        "var furl=document.getElementById('fileUrl_218');"
        "pick.addEventListener('change',function(){"
        "var f=pick.files&&pick.files[0];"
        "if(!f){b64.value='';info.textContent='未选择文件';return;}"
        "if(f.size>1048576){"
        "info.textContent=f.name+' ('+(f.size/1048576).toFixed(2)+'MB) ⚠超出1MB建议值，可能失败';"
        "}else{"
        "info.textContent=f.name+' ('+(f.size/1024).toFixed(1)+'KB)';"
        "}"
        "obj.value=f.name;"
        "var dot=f.name.lastIndexOf('.');"
        "if(dot>0){ftype.value=f.name.slice(dot+1);}"
        "furl.value='';"
        "var reader=new FileReader();"
        "reader.onload=function(){b64.value=String(reader.result).split(',')[1]||'';};"
        "reader.readAsDataURL(f);"
        "});"
        "furl.addEventListener('input',function(){"
        "if(furl.value){b64.value='';pick.value='';info.textContent='本地文件→Base64，建议<1MB';}"
        "});"
        "})();"
        "</script>"
    )

    # 左侧测试功能 + 右侧设备返回数据（sticky 固定，滚动左侧时右侧始终可见）
    content = (
        f'<div class="test-split">'
        f'<div class="test-left">{"".join(parts)}</div>'
        f'<div class="test-right">{"".join(data_parts)}</div>'
        f"</div>" + device_script + script_html
    )
    return page(
        "功能测试",
        "🧪 功能测试",
        content,
        "",  # 状态消息已由上方 msg 分支渲染为彩色提示条，避免重复显示
        active="/test",
    )


# ── POST handlers ──


@router.post("/test/cmd")
async def test_cmd(client_id: str = Form(...), cmd_type: int = Form(...)):
    if not client_id:
        return RedirectResponse(url="/test?msg=no_device", status_code=303)
    if client_id not in aivbot_service.active_connections:
        return RedirectResponse(url="/test?msg=no_robot", status_code=303)
    ok = await aivbot_service.send_command(client_id, cmd_type)
    if ok:
        return RedirectResponse(url="/test?msg=sent", status_code=303)
    return RedirectResponse(url="/test?msg=err:发送命令失败", status_code=303)


@router.post("/test/cmd_extra")
async def test_cmd_extra(request: Request):
    form = await request.form()
    client_id = form.get("client_id")
    cmd_type_str = form.get("cmd_type")
    if not client_id or not cmd_type_str:
        return RedirectResponse(url="/test?msg=err:缺少参数", status_code=303)
    if client_id not in aivbot_service.active_connections:
        return RedirectResponse(url="/test?msg=no_robot", status_code=303)

    cmd_type = int(cmd_type_str)

    # Build extra dict from non-standard form fields
    extra = {}
    skip_keys = {"client_id", "cmd_type"}
    LIST_KEYS = {"titleList", "selectList", "nameList", "removeList", "atList"}
    INT_KEYS = {"textType", "roomType", "maxRetryCount"}
    BOOL_KEYS = {"showMessageHistory"}

    for key, value in form.multi_items():
        if key in skip_keys or not value:
            continue
        if key in LIST_KEYS:
            extra[key] = [v.strip() for v in value.split(",") if v.strip()]
        elif key.startswith("friend_"):
            fkey = key[len("friend_") :]
            if "friend" not in extra:
                extra["friend"] = {}
            extra["friend"][fkey] = value
        elif key in INT_KEYS:
            try:
                extra[key] = int(value)
            except ValueError:
                extra[key] = value
        elif key in BOOL_KEYS:
            extra[key] = value.lower() in ("true", "1", "yes")
        else:
            extra[key] = value

    ok = await aivbot_service.send_command(client_id, cmd_type, extra=extra)
    if ok:
        return RedirectResponse(url="/test?msg=sent", status_code=303)
    return RedirectResponse(url="/test?msg=err:发送命令失败", status_code=303)


@router.post("/test/remark")
async def test_remark(
    client_id: str = Form(...),
    friend_name: str = Form(...),
    mark_name: str = Form(...),
):
    if not client_id:
        return RedirectResponse(url="/test?msg=no_device", status_code=303)
    if client_id not in aivbot_service.active_connections:
        return RedirectResponse(url="/test?msg=no_robot", status_code=303)
    ok = await aivbot_service.send_command(
        client_id,
        213,
        extra={"friend": {"name": friend_name, "markName": mark_name}},
    )
    if ok:
        return RedirectResponse(url="/test?msg=sent", status_code=303)
    return RedirectResponse(url="/test?msg=err:发送命令失败", status_code=303)


@router.post("/test/friend_info")
async def test_friend_info(
    client_id: str = Form(...),
    friend_name: str = Form(...),
):
    if not client_id:
        return RedirectResponse(url="/test?msg=no_device", status_code=303)
    if client_id not in aivbot_service.active_connections:
        return RedirectResponse(url="/test?msg=no_robot", status_code=303)
    ok = await aivbot_service.send_command(
        client_id,
        502,
        extra={"friend": {"name": friend_name}, "selectList": [friend_name]},
    )
    if ok:
        return RedirectResponse(url="/test?msg=sent", status_code=303)
    return RedirectResponse(url="/test?msg=err:发送命令失败", status_code=303)


@router.post("/test/send")
async def test_send(
    client_id: str = Form(...), to_name: str = Form(...), text: str = Form(...)
):
    if not client_id:
        return RedirectResponse(url="/test?msg=no_device", status_code=303)
    if client_id not in aivbot_service.active_connections:
        return RedirectResponse(url="/test?msg=no_robot", status_code=303)
    try:
        ok = await aivbot_service.send_reply(client_id, to_name, text)
        if ok:
            return RedirectResponse(url="/test?msg=sent", status_code=303)
        return RedirectResponse(url="/test?msg=err:发送失败", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/test?msg=err:{esc(str(e))}", status_code=303)
