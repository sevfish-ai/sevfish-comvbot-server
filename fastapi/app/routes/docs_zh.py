"""
中文 API 文档页面
"""

import logging
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from ..layout import page, esc

logger = logging.getLogger(__name__)

router = APIRouter(tags=["docs"])


def _section(title: str, content: str) -> str:
    return f'<div class="card"><h3>{title}</h3>{content}</div>'


def _endpoint(method: str, path: str, desc: str, detail: str = "", req_example: str = "", resp_example: str = "") -> str:
    """Format a single endpoint documentation block"""
    method_colors = {
        "GET": "#16a34a",
        "POST": "#2563eb",
        "DELETE": "#dc2626",
        "PUT": "#d97706",
        "WS": "#7c3aed",
    }
    color = method_colors.get(method, "#64748b")
    badge = f'<span class="ep-method" style="background:{color}">{method}</span>'
    path_html = f'<code class="ep-path">{path}</code>'

    req_html = ""
    if req_example:
        req_html = f'<div style="margin-top:8px"><span class="ep-label">请求示例：</span><pre>{esc(req_example)}</pre></div>'

    resp_html = ""
    if resp_example:
        resp_html = f'<div style="margin-top:8px"><span class="ep-label">响应示例：</span><pre>{esc(resp_example)}</pre></div>'

    return f"""
    <div class="ep-block">
        <div class="inline-flex">{badge}{path_html}</div>
        <div class="ep-desc">{desc}</div>
        <div class="ep-detail">{detail}</div>
        {req_html}{resp_html}
    </div>"""


@router.get("/docs/zh", response_class=HTMLResponse)
async def docs_zh_page():
    """中文 API 文档"""

    # ── 1. WebSocket 接口 ──
    ws_content = _endpoint("WS", "/webserver/wework/{client_id}",
        "Aivbot 兼容 WebSocket 主端点。设备的主要连接入口。",
        "设备连接后自动注册为在线状态，支持心跳检测、消息上报、指令下发。",
        resp_example='{"type": "welcome", "message": "Connected as xxx", "timestamp": "..."}',
    ) + _endpoint("WS", "/ws/{client_id}",
        "通用 WebSocket 端点（不验证机器人身份，用于测试）。",
        "与主端点功能相同，但不校验 robot_id 是否已注册。",
    ) + _endpoint("WS", "/ws",
        "WebSocket 端点（自动生成客户端 ID）。",
        "连接后自动分配形如 client_xxxxxx 的 ID，用于快速测试。",
    )

    # ── 2. 设备管理 API ──
    device_content = _endpoint("GET", "/api/robots",
        "列出所有已注册设备及其状态。",
        "返回设备列表 + 统计数据（总数/在线/离线/封禁数）。",
        resp_example='{"status": "success", "data": {"total": 5, "online": 2, "offline": 2, "banned": 1, "robots": [...]}}',
    ) + _endpoint("GET", "/api/robot/{robot_id}",
        "获取指定设备的详细信息。",
        "返回设备名称、状态、回复策略、最后活跃时间等。",
        resp_example='{"status": "success", "data": {"robot_id": "...", "device_name": "...", "status": "online", "reply_all": 0, ...}}',
    ) + _endpoint("POST", "/api/robot/{robot_id}/ban",
        "封禁指定设备。被封禁的设备无法连接 WebSocket。",
    ) + _endpoint("POST", "/api/robot/{robot_id}/unban",
        "解封指定设备。",
    ) + _endpoint("DELETE", "/api/robot/{robot_id}",
        "删除指定设备的注册信息。此操作不可撤销。",
    ) + _endpoint("POST", "/api/robot/{robot_id}/reply_all",
        "设置设备的回复策略（兼容接口，已废弃）。",
        "reply_all: -1=只读不回调，0=仅@回复，1=全部回调。v2 请改用「回复策略组」按四类聊天室配置。",
        req_example='{"reply_all": 1}',
        resp_example='{"status": "success", "message": "Robot xxx reply_all set to 1"}',
    )

    # ── 3. 智能体管理 API ──
    agent_content = _endpoint("GET", "/api/agents",
        "列出所有智能体。",
        "返回智能体列表（API Key 仅返回末尾 4 位）。",
        resp_example='{"status": "success", "data": [{"id": 1, "name": "客服助手", "model": "deepseek-flash", "enabled": true, ...}]}',
    ) + _endpoint("POST", "/api/agents",
        "创建智能体。",
        "字段：name(必填)、description、provider(deepseek/openai/qwen/zhipu/moonshot/custom)、base_url、model、api_key、system_prompt、temperature、max_tokens、max_context_turns、idle_reset_minutes、enabled。",
        req_example='{"name": "客服助手", "provider": "deepseek", "base_url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key": "sk-xxx", "system_prompt": "你是客服...", "max_context_turns": 20, "idle_reset_minutes": 30}',
        resp_example='{"status": "success", "message": "Agent created", "data": {"id": 1}}',
    ) + _endpoint("GET", "/api/agent/{agent_id}",
        "获取智能体详情。",
    ) + _endpoint("PUT", "/api/agent/{agent_id}",
        "更新智能体。api_key 传空串表示保留原 Key。",
    ) + _endpoint("DELETE", "/api/agent/{agent_id}",
        "删除智能体（自动解绑所有设备并删除会话记录）。",
    ) + _endpoint("POST", "/api/agent/{agent_id}/test",
        "测试智能体连接（真实调用一次 LLM，验证 key/地址/模型）。",
    ) + _endpoint("POST", "/api/robot/{robot_id}/agent",
        "将智能体绑定/解绑到设备。",
        "agent_id: 智能体 ID；传 null/0 解绑。绑定后该设备的新消息将由智能体自动回复。",
        req_example='{"agent_id": 1}',
        resp_example='{"status": "success", "message": "Robot xxx bound to agent #1"}',
    ) + _endpoint("GET", "/admin/agents",
        "智能体管理页面（新建 / 编辑 / 删除 / 测试连接）。",
        "设备管理页的「智能体」列下拉框用于绑定设备。",
    )

    # ── 4. 消息与状态 API ──
    msg_content = _endpoint("GET", "/api/status",
        "获取服务器运行状态。",
        "返回 WebSocket 连接数、消息总数、设备统计等。",
        resp_example='{"status": "success", "data": {"active_connections": 2, "message_count": 150, "robots": {...}}}',
    ) + _endpoint("GET", "/api/clients",
        "获取当前所有在线 WebSocket 客户端。",
        resp_example='{"status": "success", "data": {"count": 2, "clients": ["robot_01", "robot_02"]}}',
    ) + _endpoint("POST", "/api/send",
        "向指定设备发送消息（由设备代为发送到微信/企微）。",
        "client_id: 设备ID; received_name: 接收人/群名; text: 消息内容; room_type: 1=单聊 2=群聊",
        req_example='{"client_id": "robot_01", "received_name": "张三", "text": "你好", "room_type": 1}',
        resp_example='{"status": "success", "message": "Message sent successfully"}',
    ) + _endpoint("POST", "/api/broadcast",
        "向所有在线设备广播消息（仅测试用，设备端需实现广播协议）。",
        req_example='text=hello',
    )

    # ── 4. 分组管理 API ──
    group_content = _endpoint("GET", "/api/groups",
        "获取所有分组列表。",
        "返回每个分组的 ID、名称、成员数、创建时间。",
        resp_example='{"status": "success", "data": [{"id": 1, "name": "销售组", "member_count": 3, ...}]}',
    ) + _endpoint("POST", "/api/groups",
        "创建新分组。",
        "name: 分组名称，1-100 字符，不可重复。",
        req_example='{"name": "销售一组"}',
        resp_example='{"status": "success", "data": {"id": 1, "name": "销售一组", ...}}',
    ) + _endpoint("GET", "/api/groups/{group_id}",
        "获取分组详情，包含成员列表。",
        resp_example='{"status": "success", "data": {"id": 1, "name": "销售组", "robots": ["r1", "r2"], ...}}',
    ) + _endpoint("DELETE", "/api/groups/{group_id}",
        "删除分组（不会删除设备，仅解除分组关系）。",
    ) + _endpoint("POST", "/api/groups/{group_id}/members",
        "将设备加入分组。",
        "robot_id: 设备 ID（需已注册）。",
        req_example='{"robot_id": "robot_01"}',
    ) + _endpoint("DELETE", "/api/groups/{group_id}/members/{robot_id}",
        "将设备移出分组。",
    )

    # ── 5. WebSocket 消息类型 ──
    ws_msg_content = f"""
    <div class="doc-lead">
        设备与服务器之间通过 JSON 消息通信，所有消息都包含 <code>socketType</code> 字段。
    </div>
    <table>
        <thead>
            <tr><th>字段</th><th>值</th><th>说明</th></tr>
        </thead>
        <tbody>
            <tr><td><code>socketType</code></td><td><code>2</code></td><td>数据上报 / 指令下发（消息列表）</td></tr>
            <tr><td><code>socketType</code></td><td><code>3</code></td><td>执行结果确认（rawConfirm）</td></tr>
            <tr><td><code>type</code></td><td><code>11</code></td><td>心跳包（设备 → 服务器）</td></tr>
            <tr><td><code>td</code></td><td>时间戳</td><td>传输探测包（设备每秒发送，服务器静默忽略）</td></tr>
        </tbody>
    </table>
    """

    # ── 6. 命令类型速查表 ──
    cmd_content = """
    <div class="doc-lead">
        设备命令通过 <code>{"socketType": 2, "list": [{"type": &lt;cmd_type&gt;, ...}]}</code> 下发。
    </div>
    <table>
        <thead>
            <tr><th>类型</th><th>说明</th><th>关键参数</th></tr>
        </thead>
        <tbody>
            <tr><td><code>201</code></td><td>停止任务，返回首页</td><td>—</td></tr>
            <tr><td><code>202</code></td><td>开始接收消息</td><td>—</td></tr>
            <tr><td><code>203</code></td><td>发送消息（回复）</td><td><code>titleList</code>=[接收人], <code>receivedContent</code>=内容, <code>roomType</code>=1/2</td></tr>
            <tr><td><code>204</code></td><td>指定回复消息</td><td><code>messageId</code>=消息ID, <code>titleList</code>=[接收人], <code>receivedContent</code>=回复内容, <code>roomType</code>=1/2</td></tr>
            <tr><td><code>205</code></td><td>转发消息</td><td><code>messageId</code>=原消息ID, <code>titleList</code>=[目标], <code>roomType</code>=1/2</td></tr>
            <tr><td><code>206</code></td><td>撤回消息</td><td><code>messageId</code>=消息ID</td></tr>
            <tr><td><code>211</code></td><td>同意好友申请</td><td><code>messageId</code>=申请ID</td></tr>
            <tr><td><code>212</code></td><td>通过所有好友请求</td><td>—</td></tr>
            <tr><td><code>213</code></td><td>添加好友</td><td><code>receivedContent</code>=手机号</td></tr>
            <tr><td><code>214</code></td><td>删除好友</td><td><code>nameList</code>=[删除对象]</td></tr>
            <tr><td><code>215</code></td><td>修改好友备注</td><td><code>nameList</code>=[好友], <code>receivedContent</code>=新备注</td></tr>
            <tr><td><code>216</code></td><td>查看好友详情</td><td><code>nameList</code>=[联系人]</td></tr>
            <tr><td><code>221</code></td><td>发送名片</td><td>...</td></tr>
            <tr><td><code>222</code></td><td>打卡</td><td>—</td></tr>
            <tr><td><code>501</code></td><td>获取群信息</td><td><code>nameList</code>=[群名]</td></tr>
            <tr><td><code>503</code></td><td>获取我的信息</td><td>—</td></tr>
            <tr><td><code>505</code></td><td>获取最近聊天</td><td>—</td></tr>
            <tr><td><code>506</code></td><td>获取企业列表</td><td>—</td></tr>
            <tr><td><code>507</code></td><td>获取全部好友</td><td>—</td></tr>
            <tr><td><code>508</code></td><td>获取全部群</td><td>—</td></tr>
            <tr><td><code>509</code></td><td>获取本地文件</td><td><code>receivedContent</code>=文件路径（上传到服务器）</td></tr>
            <tr><td><code>510</code></td><td>群发助手</td><td>进入群发助手界面自动发送未发送的群发</td></tr>
            <tr><td><code>511</code></td><td>自动发表朋友圈</td><td>进入朋友圈页面自动发表待发布内容</td></tr>
            <tr><td><code>512</code></td><td>任务状态上报</td><td>设备主动上报：<code>taskStatus</code>=1 正在接收消息 / 0 已停止（设备管理页展示）</td></tr>
            <tr><td><code>701</code></td><td>发朋友圈</td><td>...</td></tr>
            <tr><td><code>801</code></td><td>发送图片</td><td><code>titleList</code>=[接收人], <code>receivedContent</code>=base64, <code>roomType</code>=1/2</td></tr>
        </tbody>
    </table>"""

    # ── 7. 设备上报数据类型 ──
    report_content = """
    <div class="doc-lead">
        设备通过 <code>{"socketType": 2, "list": [{"type": &lt;item_type&gt;, ...}]}</code> 上报数据。
    </div>
    <table>
        <thead>
            <tr><th>类型</th><th>来源</th><th>说明</th></tr>
        </thead>
        <tbody>
            <tr><td><code>101</code></td><td>消息回调</td><td>聊天消息列表（含发送者、文本、图片路径等）</td></tr>
            <tr><td><code>501</code></td><td>获取群信息</td><td>群成员列表、群公告、群主等详细信息</td></tr>
            <tr><td><code>502</code></td><td>好友详情 / 自动通过好友</td><td>好友信息（姓名、手机、备注、验证消息等）</td></tr>
            <tr><td><code>503</code></td><td>获取我的信息</td><td>当前登录用户信息</td></tr>
            <tr><td><code>505</code></td><td>获取最近聊天</td><td>最近会话列表</td></tr>
            <tr><td><code>506</code></td><td>获取企业列表</td><td>企业微信企业列表</td></tr>
            <tr><td><code>507</code></td><td>获取全部好友</td><td>完整好友列表</td></tr>
            <tr><td><code>508</code></td><td>获取全部群</td><td>完整群列表</td></tr>
        </tbody>
    </table>"""

    # ── 8. 回复策略组 ──
    reply_content = """
    <div class="doc-lead">
        回复策略组由服务器持久化。每个策略组按<b>四类聊天室</b>（内部私聊 / 外部私聊 / 内部群聊 / 外部群聊）分别配置<b>模式</b>与<b>合并窗口</b>，并携带<b>设备级限额</b>。设备绑定某个策略组后<b>立即生效</b>（引用关系）。
    </div>
    <table>
        <thead>
            <tr><th>策略</th><th>配置</th><th>限额</th><th>说明</th></tr>
        </thead>
        <tbody>
            <tr><td>默认策略<span class="cell-sub">不可修改 / 不可删除</span></td><td>私聊：回复；群聊：仅@回复（合并窗口 4 秒 / 追踪 180 秒）</td><td>并发 2 · 60s 5 次</td><td>新注册设备默认使用该策略</td></tr>
            <tr><td>新建策略</td><td>四类聊天室各自配置（模式 + 窗口，群聊含追踪时长）</td><td>可配置（≥1）</td><td>可修改、可删除；删除时使用中的设备自动回退到默认策略</td></tr>
        </tbody>
    </table>
    <div class="doc-lead" style="margin-top:12px">
        <b>模式：</b>私聊 <code>回复</code> / <code>不回复</code>；群聊 <code>追踪@回复</code>（成员 @ 后进入追踪期，追踪期内消息提交智能体，非追踪成员批次到期丢弃）/ <code>仅@回复</code>（文本含 <code>@</code> 视为被@，近似判断）/ <code>全部回复</code> / <code>不回复</code>。所有消息先进入成员级队列，合并窗口到期后<b>整批提交一次 LLM 调用</b>（同队列串行、跨队列并行）。<br><br>
        <b>设备级限额：</b>并发 LLM 调用数 ≤ <code>max_concurrent_llm</code>（默认 2）、60 秒内调用数 ≤ <code>max_calls_per_min</code>（默认 5），超限批次回队等下个窗口。<br><br>
        <b>智能体决策：</b>服务器调用该设备绑定的智能体（LLM）生成回复，智能体判定无需回复（返回空）时不发送；群聊回复以 <code>@成员名</code> 开头并携带 at 字段（设备端真提及）。未绑定智能体的设备<b>不回复</b>；绑定入口在「设备管理 → 智能体列」和「智能体管理」页面。设备管理页「推送 LLM」开关关闭 = 设备级静默（不入队、不推送、不回复）。
    </div>"""

    # ── 9. Aivbot 兼容接口 ──
    compat_content = _endpoint("GET", "/ping",
        "服务器连通性测试。",
        "Aivbot 设备端用于检测服务器是否可达。",
        resp_example='{"code": 200, "message": "success", "data": "ok"}',
    ) + _endpoint("GET", "/robot/robotInfo/get",
        "获取机器人配置。设备启动时调用。",
        "robotId: 设备 ID（URL 参数）。返回 replyAll（来自设备绑定的策略组）等配置。",
        resp_example='{"code": 200, "data": {"replyAll": 1, "openCallback": 0, ...}}',
    ) + _endpoint("POST", "/robot/robotInfo/update",
        "更新机器人配置。设备设置页修改后调用。",
        "回复策略（replyAll）由服务器端策略组管理，设备上报的 replyAll 会被忽略；其他字段（如 openCallback）仅记录日志。",
        req_example='{"robotId": "xxx", "replyAll": 1}',
    ) + _endpoint("GET", "/appUpdate/checkUpdate",
        "检查应用更新（始终返回无更新）。",
        "设备启动时调用，避免在无官方服务器的环境下反复弹更新提示。",
    ) + _endpoint("POST", "/fileUpload/upload",
        "文件上传（509 指令上传本地文件）。",
        "保存到 uploads/{robotId}/ 目录，返回可访问 URL 和本地路径。",
    ) + _endpoint("POST", "/imageUpload/upload",
        "图片上传（消息中的图片资源）。",
        "保存到 uploads/{robotId}/ 目录，返回相对路径供 WebSocket 消息引用。",
    ) + _endpoint("GET", "/fileUpload/files/{robot_id}/{filename}",
        "下载已上传的文件。",
        "用于验证文件是否上传成功。",
    ) + _endpoint("GET", "/fileUpload/list",
        "列出某设备上传过的所有文件。",
        "robotId: 设备 ID（URL 参数）。",
    )

    # ── 10. 管理页面接口 ──
    admin_content = """
    <div class="doc-lead">
        以下接口由管理页面表单提交触发，也可通过浏览器直接访问。
    </div>
    """ + _endpoint("POST", "/api/robot/register",
        "注册新设备。",
        "device_name: 设备名称; remark: 备注。",
    ) + _endpoint("POST", "/admin/robot/{robot_id}/strategy",
        "绑定设备到回复策略组（设备管理页下拉提交）。",
        "strategy_id: 策略组 ID。修改后设备即时生效。",
    ) + _endpoint("POST", "/admin/robot/{robot_id}/reply_all",
        "兼容接口：直接修改设备 reply_all 列。",
        "reply_all: -1=只读 0=仅@ 1=全部（新页面请用策略组绑定）",
    ) + _endpoint("GET", "/admin/strategies",
        "回复策略组管理页（列表 / 新建 / 编辑 / 删除）。",
        "默认策略不可修改、不可删除；删除策略时使用中的设备回退到默认策略。",
    ) + _endpoint("POST", "/admin/strategy/create",
        "新建回复策略组。",
        "name: 策略名称; mode_1/merge_window_1/track_duration_1…（四类聊天室模式与合并窗口，群聊含追踪时长）; max_concurrent_llm: 设备并发上限; max_calls_per_min: 60s 调用上限。",
    ) + _endpoint("POST", "/admin/strategy/{strategy_id}/update",
        "修改回复策略组（默认策略不可修改）。",
        "修改后绑定该策略的所有设备自动生效。",
    ) + _endpoint("GET", "/admin/strategy/{strategy_id}/delete",
        "删除回复策略组（默认策略不可删除）。",
        "使用该策略的设备自动回退到默认策略。",
    ) + _endpoint("GET", "/admin/ban/{robot_id}",
        "封禁设备（重定向回管理页）。",
    ) + _endpoint("GET", "/admin/unban/{robot_id}",
        "解封设备（重定向回管理页）。",
    ) + _endpoint("GET", "/admin/delete/{robot_id}",
        "删除设备（重定向回管理页，不可撤销）。",
    )

    # Assemble page
    content = f"""
    <div class="doc-lead" style="margin-bottom:16px;color:var(--text-2)">
        本文档覆盖所有已实现接口，与当前代码完全一致。
        <span class="badge badge-online" style="margin-left:12px">✅ 最后审查：2026-07-31</span>
    </div>

    {_section("🔗 WebSocket 接口", ws_content)}
    {_section("📱 设备管理 API", device_content)}
    {_section("🤖 智能体管理 API", agent_content)}
    {_section("💬 消息与状态 API", msg_content)}
    {_section("📦 分组管理 API", group_content)}
    {_section("🤝 Aivbot 兼容接口", compat_content)}
    {_section("🔧 管理操作接口", admin_content)}
    {_section("📋 WebSocket 消息格式", ws_msg_content)}
    {_section("📜 命令类型速查表", cmd_content)}
    {_section("📊 设备上报数据类型", report_content)}
    {_section("⚙️ 回复策略说明", reply_content)}
    """

    return page(
        "中文 API 文档",
        "📖 中文 API 文档",
        content,
        active="/docs/zh",
    )