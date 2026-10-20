"""
Admin dashboard route (server-side rendered)
"""

import asyncio
import logging
from typing import List
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from ..services import robot_service, agent_service, group_service, memory_service
from ..services.aivbot_service import aivbot_service
from ..layout import page, esc, _status_badge, _fmt_dt


logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


def _status_text(s: str) -> str:
    return {"online": "在线", "offline": "离线", "banned": "已封禁"}.get(s, s)


def _redirect_url(base: str, org_id: int, msg: str) -> str:
    """构建带 org_id 与 msg 的重定向 URL。
    修复：org_id=0 时用 `?` 分隔，避免 `/admin/strategies&msg=…` 404。"""
    url = base
    if org_id:
        url += f"?org_id={org_id}"
    return f"{url}{'&' if org_id else '?'}msg={msg}"


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, msg: str = "", org_id: int = 0):
    """Admin dashboard — server-side rendered with sidebar layout（支持 ?org_id= 按组织过滤）"""
    all_robots = await robot_service.list_robots()
    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if not grp:
            return RedirectResponse(
                url="/admin/orgs?msg=❌ 组织不存在", status_code=303
            )
        cur_org_name = grp["name"]
        member_ids = set(grp["members"].get("device", []))
        all_robots = [r for r in all_robots if r.robot_id in member_ids]

    robots = all_robots
    strategies = await robot_service.list_strategies()
    agents = await agent_service.list_agents()
    # 长期记忆集：仅同组织（org_id=0 时展示全部）
    all_mems = await memory_service.list_sets()
    if org_id:
        org_mem_ids = {int(x) for x in await group_service.get_group_org_members(org_id, "memory")}
        memory_sets = [m for m in all_mems if m["id"] in org_mem_ids]
    else:
        memory_sets = all_mems
    mem_names = {m["id"]: m["name"] for m in all_mems}

    # ── Stats cards ──
    # 在线口径 = **WebSocket 连接注册表**（唯一事实来源），不是 robots.status 那列：
    # DB 列会漂移（掉线自愈、兼容端点不回写都会让"注册表有连接、DB 却写 offline"），
    # 之前统计卡片用 DB、行内徽标用注册表，同一页两个口径对不上。
    connected = set(aivbot_service.active_connections)
    total = len(robots)
    banned = sum(1 for r in robots if r.status == "banned")
    online = sum(
        1 for r in robots if r.status != "banned" and r.robot_id in connected
    )
    offline = max(total - online - banned, 0)

    stats_cards = f"""
    <div class="stat-grid">
        <div class="stat-card">
            <span class="stat-icon">📱</span>
            <div class="stat-num">{total}</div>
            <div class="stat-label">设备总数</div>
        </div>
        <div class="stat-card">
            <span class="stat-icon">🟢</span>
            <div class="stat-num" style="color:var(--green)">{online}</div>
            <div class="stat-label">在线</div>
        </div>
        <div class="stat-card">
            <span class="stat-icon">⚪</span>
            <div class="stat-num" style="color:var(--text-2)">{offline}</div>
            <div class="stat-label">离线</div>
        </div>
        <div class="stat-card">
            <span class="stat-icon">🚫</span>
            <div class="stat-num" style="color:var(--red)">{banned}</div>
            <div class="stat-label">已封禁</div>
        </div>
    </div>"""

    # ── Register form ──
    register_form = f"""
    <form method="POST" action="/api/robot/register" class="form-row">
        <input type="hidden" name="org_id" value="{org_id}">
        <div class="form-group">
            <label>设备名称</label>
            <input type="text" name="device_name" placeholder="例如：模拟器1号" value="">
        </div>
        <div class="form-group">
            <label>备注</label>
            <input type="text" name="remark" placeholder="备注信息" value="">
        </div>
        <button type="submit" class="btn btn-primary">➕ 注册新设备</button>
    </form>"""

    # ── Table ──
    if not robots:
        rows_html = '<tr><td colspan="14" class="empty">暂无注册设备</td></tr>'
    else:
        rows_parts = []
        for r in robots:
            rid = esc(r.robot_id)
            dname = esc(r.device_name)
            remark = esc(r.remark)
            # 状态列：优先真实 WebSocket 连接；DB 标记在线但注册表暂无连接时显示「在线(DB)」，
            # 避免与统计卡片(DB status)不一致而误报离线
            if r.status == "banned":
                badge = '<span class="badge badge-offline">已封禁</span>'
            elif r.robot_id in aivbot_service.active_connections:
                badge = '<span class="badge badge-online">🟢 在线</span>'
            elif r.status == "online":
                badge = '<span class="badge badge-online">🟢 在线(DB)</span>'
            else:
                badge = '<span class="badge badge-offline">离线</span>'
            last_seen = _fmt_dt(r.last_seen)
            created = _fmt_dt(r.created_at)

            # Reply strategy group dropdown (default strategy first)
            opts_html = "".join(
                f'<option value="{s.id}"{" selected" if r.reply_strategy_id == s.id else ""}>'
                f'{esc(s.name)}{"（默认）" if s.is_default else ""}</option>'
                for s in strategies
            )
            reply_html = f"""
                <form method="POST" action="/admin/robot/{rid}/strategy{f'?org_id={org_id}' if org_id else ''}" class="inline-form">
                    <select name="strategy_id" class="sel-sm" onchange="this.form.submit()">
                        {opts_html}
                    </select>
                </form>
                <div style="margin-top:4px">
                    <a href="/admin/strategies{f'?org_id={org_id}' if org_id else ''}" class="btn btn-ghost btn-sm">✏️ 编辑</a>
                </div>"""

            # 绑定智能体下拉（0=未绑定）
            agent_opts = '<option value="0">未绑定</option>' + "".join(
                f'<option value="{a.id}"{" selected" if r.agent_id == a.id else ""}>'
                f'{esc(a.name)}{"" if a.enabled else "（停用）"}</option>'
                for a in agents
            )
            agent_html = f"""
                <form method="POST" action="/admin/robot/{rid}/agent{f'?org_id={org_id}' if org_id else ''}" class="inline-form">
                    <select name="agent_id" class="sel-sm" style="max-width:150px" onchange="this.form.submit()">
                        {agent_opts}
                    </select>
                </form>"""

            # 本机在企业微信里的名字：用于群聊"是否真的 @ 了我"的判定。
            # 只存在内存（来自 503 回执）→ 重启后为空，所以给一个手动获取按钮，
            # 不做自动获取（避免上线/重启时打扰设备界面，也避免名字过期导致漏 @）。
            self_name = aivbot_service.get_self_name(r.robot_id)
            self_name_html = (
                f'<div style="font-size:12px"><code>{esc(self_name)}</code></div>'
                f'<div class="muted" style="font-size:11px">用于群聊 @ 判定</div>'
                if self_name
                else '<div class="muted" style="font-size:12px">—（未获取）</div>'
            ) + (
                f'<form method="POST" action="/admin/robot/{rid}/self_name'
                f'{f"?org_id={org_id}" if org_id else ""}" style="display:inline;margin-top:4px">'
                f'<button class="btn btn-ghost btn-sm" type="submit">获取名字</button></form>'
            )

            # 长期记忆（挂在设备上：换智能体不换记忆，解绑设备即失效）
            mem_link = await memory_service.get_robot_memory_sets(r.robot_id)
            mem_write = mem_link.get("write")
            mem_label = (
                f"维护：{esc(mem_names.get(mem_write, '#' + str(mem_write)))}"
                if mem_write
                else '<span class="muted">未启用</span>'
            )
            mem_read = len(mem_link.get("read") or [])
            memory_html = (
                f'<div style="font-size:12px">{mem_label}'
                f'{"<br><span class=\"muted\">只读 " + str(mem_read) + " 套</span>" if mem_read else ""}</div>'
                f'<div style="margin-top:4px"><a class="btn btn-ghost btn-sm" '
                f'href="/admin/robot/{rid}/memory{f"?org_id={org_id}" if org_id else ""}">🧠 设置</a></div>'
            )

            # 任务状态（是否在接收消息，来自设备端 512 上报 / 服务端 202/201 乐观同步）
            running = aivbot_service.task_status.get(r.robot_id)
            if running is None:
                task_status_html = '<span class="badge badge-offline">未知</span>'
            elif running:
                task_status_html = '<span class="badge badge-online">🔴 接收中</span>'
            else:
                task_status_html = '<span class="badge badge-offline">⏹ 已停止</span>'
            task_html = f"""
                <div>{task_status_html}</div>
                <div style="margin-top:6px;white-space:nowrap">
                    <form method="POST" action="/admin/robot/{rid}/start_receive{f'?org_id={org_id}' if org_id else ''}" style="display:inline">
                        <button type="submit" class="btn btn-success btn-sm">▶ 开始</button>
                    </form>
                    <form method="POST" action="/admin/robot/{rid}/stop_receive{f'?org_id={org_id}' if org_id else ''}" style="display:inline">
                        <button type="submit" class="btn btn-ghost btn-sm">⏹ 停止</button>
                    </form>
                </div>"""

            # 推送 LLM 开关（服务器管理，设备端只读；开启=消息队列推送给智能体，智能体自主判断是否回复；关闭=设备级静默，不入队不推送）
            ar_on = bool(r.auto_reply)
            auto_reply_html = f"""
                <div><span class="badge {'badge-online' if ar_on else 'badge-offline'}">{'开启' if ar_on else '关闭'}</span></div>
                <form method="POST" action="/admin/robot/{rid}/auto_reply{f'?org_id={org_id}' if org_id else ''}" style="margin-top:6px" class="inline-form">
                    <select name="auto_reply" class="sel-sm" onchange="this.form.submit()">
                        <option value="1"{' selected' if ar_on else ''}>开启</option>
                        <option value="0"{' selected' if not ar_on else ''}>关闭</option>
                    </select>
                </form>"""

            if r.status == "banned":
                actions = f"""
                    <a href="/admin/unban/{rid}{f'?org_id={org_id}' if org_id else ''}" class="btn btn-success btn-sm">解封</a>
                    <a href="/admin/delete/{rid}{f'?org_id={org_id}' if org_id else ''}" class="btn btn-danger btn-sm"
                       onclick="return confirm('确认删除 {rid}？此操作不可撤销！')">删除</a>"""
            else:
                actions = f"""
                    <a href="/admin/ban/{rid}{f'?org_id={org_id}' if org_id else ''}" class="btn btn-danger btn-sm"
                       onclick="return confirm('确认封禁 {rid}？')">封禁</a>
                    <a href="/admin/delete/{rid}{f'?org_id={org_id}' if org_id else ''}" class="btn btn-ghost btn-sm"
                       onclick="return confirm('确认删除 {rid}？此操作不可撤销！')">删除</a>"""

            rows_parts.append(
                f"<tr>"
                f"<td><input type='checkbox' class='row-check' name='robot_ids' value='{rid}' form='batchForm'></td>"
                f"<td><code>{rid}</code></td>"
                f"<td>{dname}</td>"
                f"<td>{remark}</td>"
                f"<td>{badge}</td>"
                f"<td>{task_html}</td>"
                f"<td>{auto_reply_html}</td>"
                f"<td>{reply_html}</td>"
                f"<td>{agent_html}</td>"
                f"<td>{self_name_html}</td>"
                f"<td>{memory_html}</td>"
                f"<td>{last_seen}</td>"
                f"<td>{created}</td>"
                f"<td>{actions}</td>"
                f"</tr>"
            )
        rows_html = "\n".join(rows_parts)

    table_html = f"""
    <div class="table-wrap">
    <table>
        <thead>
            <tr><th style="width:36px"></th><th>Robot ID</th><th>设备名称</th><th>备注</th><th>状态</th><th>任务状态</th><th>推送 LLM</th><th>回复策略</th><th>智能体</th><th>企微名</th><th>长期记忆</th><th>最后活跃</th><th>注册时间</th><th>操作</th></tr>
        </thead>
        <tbody>{rows_html}</tbody>
    </table>
    </div>"""

    batch_bar = f"""
    <form method="POST" action="/admin/robots/batch{f'?org_id={org_id}' if org_id else ''}" id="batchForm" class="inline-flex" style="margin-bottom:14px;gap:8px" onsubmit="return requireChecked()">
        <span style="font-size:13px;color:var(--text-2)">已选 <b id="selCount" style="color:var(--primary)">0</b> 台</span>
        <button type="submit" formaction="/admin/robots/batch?action=start{f'&org_id={org_id}' if org_id else ''}" class="btn btn-success btn-sm">▶ 批量开始</button>
        <button type="submit" formaction="/admin/robots/batch?action=stop{f'&org_id={org_id}' if org_id else ''}" class="btn btn-ghost btn-sm">⏹ 批量停止</button>
        <button type="submit" formaction="/admin/robots/batch?action=auto_reply_on{f'&org_id={org_id}' if org_id else ''}" class="btn btn-primary btn-sm">✅ 批量开启推送</button>
        <button type="submit" formaction="/admin/robots/batch?action=auto_reply_off{f'&org_id={org_id}' if org_id else ''}" class="btn btn-ghost btn-sm">🚫 批量关闭推送</button>
        <label class="form-check" style="margin-left:6px"><input type="checkbox" id="checkAll" onchange="toggleAll(this)"> 全选</label>
    </form>
    <script>
    function requireChecked() {{
        var checks = document.querySelectorAll('input.row-check:checked');
        if (checks.length === 0) {{ alert('请先勾选设备'); return false; }}
        return true;
    }}
    function toggleAll(cb) {{
        var checks = document.querySelectorAll('input.row-check');
        for (var i = 0; i < checks.length; i++) {{ checks[i].checked = cb.checked; }}
        updateCount();
    }}
    function updateCount() {{
        var n = document.querySelectorAll('input.row-check:checked').length;
        var el = document.getElementById('selCount');
        if (el) el.textContent = n;
    }}
    document.addEventListener('DOMContentLoaded', function () {{
        var checks = document.querySelectorAll('input.row-check');
        for (var i = 0; i < checks.length; i++) {{
            checks[i].addEventListener('change', updateCount);
        }}
    }});
    </script>"""

    content = f"""
    <div class="card">
        <h3>📊 设备概览</h3>
        {stats_cards}
    </div>
    <div class="card">
        <h3>➕ 注册新设备</h3>
        {register_form}
    </div>
    <div class="card">
        <h3>📋 设备列表</h3>
        {batch_bar}
        {table_html}
    </div>"""

    return page(
        "设备管理", "📱 设备管理", content, msg, active="devices",
        org_id=org_id, org_name=cur_org_name, org_active="devices",
    )


# ── Admin action endpoints (perform action & redirect back) ──


@router.post("/admin/robot/{robot_id}/strategy")
async def admin_robot_strategy(robot_id: str, strategy_id: int = Form(...), org_id: int = Query(0)):
    """Bind a robot to a reply strategy group (form POST from admin page)."""
    base = "/admin"
    try:
        success = await robot_service.update_robot_strategy(robot_id, strategy_id)
        if not success:
            logger.warning(f"robot strategy update failed for {robot_id}: not found")
            return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 设备或策略不存在"), status_code=303)
        logger.info(f"✅ admin_robot_strategy: {robot_id} -> strategy #{strategy_id}")
    except Exception as e:
        logger.error(f"robot strategy update failed for {robot_id}: {e}")
        return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 更新失败: {esc(str(e))}"), status_code=303)
    return RedirectResponse(url=_redirect_url(base, org_id, f"✅ 回复策略已更新"), status_code=303)


@router.post("/admin/robot/{robot_id}/agent")
async def admin_robot_agent(robot_id: str, agent_id: int = Form(...), org_id: int = Query(0)):
    """Bind/unbind a robot to an agent (form POST from admin page). 0 = unbind."""
    base = "/admin"
    try:
        if agent_id <= 0:
            success = await agent_service.unbind_agent(robot_id)
            action = "解绑"
        else:
            success = await agent_service.bind_agent(robot_id, agent_id)
            action = "绑定"
        if not success:
            logger.warning(f"robot agent update failed for {robot_id}")
            return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 设备或智能体不存在"), status_code=303)
        logger.info(f"✅ admin_robot_agent: {robot_id} {action} agent #{agent_id}")
    except Exception as e:
        logger.error(f"robot agent update failed for {robot_id}: {e}")
        return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 更新失败: {esc(str(e))}"), status_code=303)
    return RedirectResponse(url=_redirect_url(base, org_id, f"✅ 智能体已更新"), status_code=303)


@router.get("/admin/robot/{robot_id}/memory", response_class=HTMLResponse)
async def admin_robot_memory(robot_id: str, org_id: int = Query(0), msg: str = ""):
    """设备的长期记忆绑定页：维护集单选（写入这里）+ 只读集多选（可跨集读取）。

    记忆挂在设备上：换智能体不影响记忆，解绑设备即自动失效（这里不用做任何清理）。
    """
    robot = await robot_service.get_robot(robot_id)
    if not robot:
        return RedirectResponse(
            url=_redirect_url("/admin", org_id, "❌ 设备不存在"), status_code=303
        )
    org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        org_name = grp["name"] if grp else ""
    all_mems = await memory_service.list_sets()
    if org_id:
        org_mem_ids = {int(x) for x in await group_service.get_group_org_members(org_id, "memory")}
        mems = [m for m in all_mems if m["id"] in org_mem_ids]
    else:
        mems = all_mems

    link = await memory_service.get_robot_memory_sets(robot_id)
    write_id = link.get("write") or 0
    read_ids = set(link.get("read") or [])
    back = f"/admin?org_id={org_id}" if org_id else "/admin"

    if not mems:
        options = (
            '<p class="form-hint">当前组织暂无记忆集。请先到'
            f'<a href="/admin/memory{f"?org_id={org_id}" if org_id else ""}">「长期记忆」</a>页创建，'
            "或把已有记忆集加入本组织。</p>"
        )
    else:
        write_opts = "".join(
            f'<label class="chip-check"><input type="radio" name="memory_write" value="{m["id"]}"'
            f'{(" checked" if int(m["id"]) == int(write_id) else "")}> {esc(m["name"])}</label>'
            for m in mems
        )
        read_opts = "".join(
            f'<label class="chip-check"><input type="checkbox" name="memory_read_ids" value="{m["id"]}"'
            f'{(" checked" if int(m["id"]) in read_ids and int(m["id"]) != int(write_id) else "")}>'
            f' {esc(m["name"])}</label>'
            for m in mems
        )
        options = f"""
        <div class="form-group full">
            <label>维护集（单选：这台设备上的智能体把客户事实写到这里；不选 = 不能写，只能读）</label>
            <div class="inline-flex" style="gap:8px;flex-wrap:wrap">
                <label class="chip-check"><input type="radio" name="memory_write" value="0"
                    {(" checked" if not write_id else "")}> 不维护（不使用记忆 / 只读）</label>
                {write_opts}
            </div>
        </div>
        <div class="form-group full">
            <label>只读集（多选：可以读取这些记忆集的客户事实，但没有写权限）</label>
            <div class="inline-flex" style="gap:8px;flex-wrap:wrap">{read_opts}</div>
            <div class="form-hint">记忆跟着设备走：同一台设备换智能体，客户记忆不变；把设备解绑智能体，
            记忆自动不再注入（配置仍保留在此，随时可再启用）。智能体自己写入的一条事实 ≤200 字，
            其余护栏阈值与用量见「长期记忆」页。</div>
        </div>"""

    content = f"""
    <div class="card">
        <h3>🧠 设备长期记忆 · <code>{esc(robot_id)}</code></h3>
        <p class="card-sub">设备：<b>{esc(robot.device_name or robot_id)}</b>
        {f'（备注：{esc(robot.remark)}）' if robot.remark else ''}
        ｜当前绑定智能体：<b>{esc(robot.agent_name or '未绑定')}</b></p>
        <form method="POST" action="/admin/robot/{esc(robot_id)}/memory{f'?org_id={org_id}' if org_id else ''}"
              class="form-stack">
            {options}
            <div class="inline-flex" style="gap:8px;margin-top:12px">
                <button class="btn btn-primary">保存</button>
                <a class="btn btn-ghost" href="{back}">返回设备列表</a>
            </div>
        </form>
    </div>"""
    return page(
        f"{robot_id} 长期记忆", f"🧠 {esc(robot.device_name or robot_id)} · 长期记忆", content, msg,
        org_id=org_id, org_name=org_name, org_active="devices",
    )


@router.post("/admin/robot/{robot_id}/memory")
async def admin_robot_memory_save(
    robot_id: str,
    org_id: int = Query(0),
    memory_write: int = Form(0),
    memory_read_ids: List[int] = Form([]),
):
    try:
        if not await robot_service.get_robot(robot_id):
            raise ValueError(f"设备 {robot_id} 不存在")
        await memory_service.set_robot_memory_sets(robot_id, memory_write, memory_read_ids)
        msg = "✅ 已保存" if memory_write else "✅ 已保存（未设维护集，记忆只读/不使用）"
    except Exception as e:
        logger.error(f"robot memory save failed {robot_id}: {e}")
        msg = f"❌ 保存失败: {esc(str(e))}"
    url = f"/admin/robot/{robot_id}/memory"
    return RedirectResponse(url=_redirect_url(url, org_id, msg), status_code=303)


# 「获取名字」等待回执的参数（抽成常量便于测试压缩等待时间）
SELF_NAME_POLL = 0.5
SELF_NAME_TRIES = 24


@router.post("/admin/robot/{robot_id}/self_name")
async def admin_robot_self_name(robot_id: str, org_id: int = Query(0)):
    """让设备上报"我在企业微信里的名字"（503 我的信息），用于群聊 @ 判定。

    回执是异步的，这里最多等 12 秒；拿到就提示名字，没拿到也提示"稍后刷新"，
    因为设备可能正忙于收发消息（不重试、不排队，避免干扰正在进行的会话）。
    """
    ok = await aivbot_service.send_command(robot_id, 503)
    if not ok:
        return RedirectResponse(
            url=_redirect_url("/admin", org_id, "❌ 设备离线，无法下发指令"), status_code=303
        )
    name = ""
    for _ in range(SELF_NAME_TRIES):  # 默认 0.5s × 24 = 12s
        await asyncio.sleep(SELF_NAME_POLL)
        name = aivbot_service.get_self_name(robot_id)
        if name:
            break
    msg = (
        f"✅ 已获取企微名：{name}"
        if name
        else "⚠️ 已下发获取指令，但未收到回执（设备可能正忙），稍后刷新列表查看"
    )
    return RedirectResponse(url=_redirect_url("/admin", org_id, msg), status_code=303)


@router.post("/admin/robot/{robot_id}/start_receive")
async def admin_start_receive(robot_id: str, org_id: int = Query(0)):
    """下发 202 开始接收消息（与功能测试页「开始接收消息」一致）"""
    ok = await aivbot_service.send_command(robot_id, 202)
    msg = "✅ 已下发开始接收指令" if ok else "❌ 设备离线，无法下发指令"
    base = "/admin"
    return RedirectResponse(url=_redirect_url(base, org_id, f"{esc(msg)}"), status_code=303)


@router.post("/admin/robot/{robot_id}/stop_receive")
async def admin_stop_receive(robot_id: str, org_id: int = Query(0)):
    """下发 201 停止任务回首页（与功能测试页「停止任务回首页」一致）"""
    ok = await aivbot_service.send_command(robot_id, 201)
    msg = "✅ 已下发停止指令" if ok else "❌ 设备离线，无法下发指令"
    base = "/admin"
    return RedirectResponse(url=_redirect_url(base, org_id, f"{esc(msg)}"), status_code=303)


@router.post("/admin/robot/{robot_id}/auto_reply")
async def admin_auto_reply(robot_id: str, auto_reply: int = Form(default=1), org_id: int = Query(0)):
    """更新设备自动回复开关（服务端管理，关闭后不调用智能体也不发送）"""
    base = "/admin"
    try:
        success = await robot_service.update_auto_reply(robot_id, bool(auto_reply))
        if not success:
            logger.warning(f"auto_reply update failed for {robot_id}: not found")
            return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 设备不存在"), status_code=303)
        logger.info(f"✅ admin_auto_reply: {robot_id} -> {auto_reply}")
    except Exception as e:
        logger.error(f"auto_reply update failed for {robot_id}: {e}")
        return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 更新失败: {esc(str(e))}"), status_code=303)
    return RedirectResponse(url=_redirect_url(base, org_id, f"✅ 自动回复已更新"), status_code=303)


@router.post("/admin/robots/batch")
async def admin_robots_batch(
    robot_ids: List[str] = Form(...), action: str = Query(...), org_id: int = Query(0)
):
    """批量操作（设备管理页勾选后执行）：
    start=批量开始接收(202) / stop=批量停止(201)
    auto_reply_on=批量开启自动回复 / auto_reply_off=批量关闭自动回复
    """
    base = "/admin"
    ok_count = 0
    fail_count = 0
    for rid in robot_ids:
        try:
            if action == "start":
                ok = await aivbot_service.send_command(rid, 202)
            elif action == "stop":
                ok = await aivbot_service.send_command(rid, 201)
            elif action == "auto_reply_on":
                ok = await robot_service.update_auto_reply(rid, True)
            elif action == "auto_reply_off":
                ok = await robot_service.update_auto_reply(rid, False)
            else:
                return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 未知操作"), status_code=303)
            if ok:
                ok_count += 1
            else:
                fail_count += 1
                logger.warning(f"batch {action} failed for {rid}")
        except Exception as e:
            logger.error(f"batch {action} error for {rid}: {e}")
            fail_count += 1
    msg = f"✅ 成功 {ok_count} 台" + (f"，失败 {fail_count} 台" if fail_count else "")
    logger.info(f"✅ batch {action}: ok={ok_count} fail={fail_count}")
    return RedirectResponse(url=_redirect_url(base, org_id, f"{esc(msg)}"), status_code=303)


@router.post("/admin/robot/{robot_id}/reply_all")
async def admin_reply_all(robot_id: str, reply_all: int = Form(default=0), org_id: int = Query(0)):
    """Legacy: change robot reply_all column directly (kept for compatibility)."""
    base = "/admin"
    try:
        success = await robot_service.update_reply_all(robot_id, reply_all)
        if not success:
            logger.warning(f"reply_all update failed for {robot_id}: robot not found")
            return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 设备不存在"), status_code=303)
        logger.info(f"✅ admin_reply_all: {robot_id} -> {reply_all}")
    except ValueError as e:
        logger.error(f"reply_all invalid value for {robot_id}: {e}")
        return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 无效的回复策略值: {esc(str(e))}"), status_code=303)
    except Exception as e:
        logger.error(f"reply_all update failed for {robot_id}: {e}")
        return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 更新失败: {esc(str(e))}"), status_code=303)
    return RedirectResponse(url=_redirect_url(base, org_id, f"✅ 回复策略已更新"), status_code=303)


# ── Reply strategy group management ─────────────────────

# 四类聊天室（room_type: "1"=外部群 "2"=外部私聊 "3"=内部群 "4"=内部私聊）
# 私聊模式：reply / silent；群聊模式：track_at / at_only / reply_all / silent
ROOM_DEFS = (
    ("1", "外群", True),
    ("2", "外私", False),
    ("3", "内群", True),
    ("4", "内私", False),
)
MODE_LABELS = {
    "track_at": "追踪@",
    "at_only": "仅@",
    "reply_all": "全部回复",
    "reply": "回复",
    "silent": "不回复",
}
GROUP_MODES = (("track_at", "追踪@"), ("at_only", "仅@"), ("reply_all", "全部回复"), ("silent", "不回复"))
PRIVATE_MODES = (("reply", "回复"), ("silent", "不回复"))


def _strategy_summary(s) -> str:
    """四类聊天室模式摘要，如 外群:仅@(窗4s) / 外私:回复(窗4s) / 内群:追踪@(窗4s·追180s) / 内私:回复(窗4s)。
    追踪时长仅在群聊模式为「追踪@」时展示（其它模式不生效，避免误解）。"""
    cfg = s.config if isinstance(s.config, dict) else {}
    parts = []
    for rt, rlabel, is_group in ROOM_DEFS:
        rc = cfg.get(rt) or {}
        mode = rc.get("mode", "")
        label = MODE_LABELS.get(mode, mode or "-")
        detail = f"(窗{rc.get('merge_window', '-')}s"
        if is_group and mode == "track_at":
            detail += f"·追{rc.get('track_duration', '-')}s"
        detail += ")"
        parts.append(f"{rlabel}:{label}{detail}")
    return " / ".join(parts)


def _strategy_limits(s) -> str:
    return f"并发{s.max_concurrent_llm} · 60s{s.max_calls_per_min}次"


def _mode_select(name: str, current: str, opts, onchange: str = "") -> str:
    return "<select name='%s'%s>%s</select>" % (
        name,
        f" onchange=\"{onchange}\"" if onchange else "",
        "".join(
            f"<option value='{v}'{' selected' if v == current else ''}>{label}</option>"
            for v, label in opts
        ),
    )


def _room_matrix_rows(prefill: dict) -> str:
    """四类聊天室配置矩阵行：模式下拉 + 合并窗口(秒) + 群聊追踪时长(秒)。
    表单字段名固定 mode_N / merge_window_N / track_duration_N（N=1..4）。
    追踪时长仅群聊模式选「追踪@」时可编辑（其它模式禁用，避免误解）。"""
    rows = []
    for rt, rlabel, is_group in ROOM_DEFS:
        rc = prefill.get(rt, {}) if isinstance(prefill, dict) else {}
        mw = rc.get("merge_window", 4)
        current_mode = rc.get("mode", "at_only" if is_group else "reply")
        msel = _mode_select(
            f"mode_{rt}",
            current_mode,
            GROUP_MODES if is_group else PRIVATE_MODES,
            onchange=(
                f"document.getElementsByName('track_duration_{rt}')[0].disabled = "
                f"this.value !== 'track_at'"
                if is_group
                else ""
            ),
        )
        mw_in = f"<input type='number' name='merge_window_{rt}' value='{mw}' min='1' required>"
        if is_group:
            track_disabled = "" if current_mode == "track_at" else " disabled"
            hint = "" if current_mode == "track_at" else "（仅追踪@模式生效）"
            rows.append(
                f"<tr><td>{rlabel}</td><td>{msel}</td><td>{mw_in}</td>"
                f"<td><input type='number' name='track_duration_{rt}' value='{rc.get('track_duration', 180)}'"
                f" min='0'{track_disabled}>"
                f"<span class='muted' style='font-size:11px'>{hint}</span></td></tr>"
            )
        else:
            rows.append(f"<tr><td>{rlabel}</td><td>{msel}</td><td>{mw_in}</td><td></td></tr>")
    return "\n".join(rows)


def _config_from_form(f: dict) -> dict:
    """从表单字段组装 config dict（字段名 mode_N / merge_window_N / track_duration_N）。
    track_duration 仅群聊收集；模式非「追踪@」时输入框被禁用不提交，由路由默认值兜底。"""
    cfg = {
        "1": {"mode": f["mode_1"], "merge_window": f["merge_window_1"],
              "track_duration": f["track_duration_1"]},
        "2": {"mode": f["mode_2"], "merge_window": f["merge_window_2"]},
        "3": {"mode": f["mode_3"], "merge_window": f["merge_window_3"],
              "track_duration": f["track_duration_3"]},
        "4": {"mode": f["mode_4"], "merge_window": f["merge_window_4"]},
    }
    return cfg


@router.get("/admin/strategies", response_class=HTMLResponse)
async def admin_strategies(request: Request, msg: str = "", org_id: int = Query(0)):
    """Reply strategy group management page (list / create / edit / delete)."""
    strategies = await robot_service.list_strategies()

    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if not grp:
            return RedirectResponse(
                url="/admin/orgs?msg=❌ 组织不存在", status_code=303
            )
        cur_org_name = grp["name"]

    rows_parts = []
    org_hidden = f"<input type='hidden' name='org_id' value='{org_id}'>" if org_id else ""
    for s in strategies:
        sid = s.id
        if s.is_default:
            rows_parts.append(
                f"<tr>"
                f"<td>{esc(s.name)} <span class='badge badge-online'>默认</span></td>"
                f"<td>{_strategy_summary(s)}</td>"
                f"<td>{_strategy_limits(s)}</td>"
                f"<td>{s.robot_count}</td>"
                f"<td style='color:#94a3b8'>不可修改 / 不可删除</td>"
                f"</tr>"
            )
        else:
            rows_parts.append(
                f"<tr>"
                f"<td>{esc(s.name)}</td>"
                f"<td>{_strategy_summary(s)}</td>"
                f"<td>{_strategy_limits(s)}</td>"
                f"<td>{s.robot_count}</td>"
                f"<td>"
                f"<a href='#edit-{sid}' class='btn btn-primary btn-sm' onclick=\"toggleEdit({sid})\">编辑</a> "
                f"<a href='/admin/strategy/{sid}/delete' class='btn btn-danger btn-sm' "
                f"onclick=\"return confirm('删除策略「{esc(s.name)}」？使用该策略的 {s.robot_count} 台设备将回退到默认策略！')\">删除</a>"
                f"</td>"
                f"</tr>"
                f"<tr id='edit-{sid}' style='display:none;background:#f8fafc'>"
                f"<td colspan='5'>"
                f"<form method='POST' action='/admin/strategy/{sid}/update'>"
                f"{org_hidden}"
                f"<div class='form-row' style='margin:0'>"
                f"<div class='form-group'><label>策略名称</label>"
                f"<input type='text' name='name' value='{esc(s.name)}' required></div>"
                f"<div class='form-group'><label>并发 LLM</label>"
                f"<input type='number' name='max_concurrent_llm' value='{s.max_concurrent_llm}' min='1' required></div>"
                f"<div class='form-group'><label>60s 调用上限</label>"
                f"<input type='number' name='max_calls_per_min' value='{s.max_calls_per_min}' min='1' required></div>"
                f"</div>"
                f"<table style='margin-top:10px'>"
                f"<thead><tr><th>聊天室</th><th>模式</th><th>合并窗口(秒)</th><th>追踪时长(秒)</th></tr></thead>"
                f"<tbody>{_room_matrix_rows(s.config)}</tbody>"
                f"</table>"
                f"<div style='margin-top:10px'>"
                f"<button type='submit' class='btn btn-primary'>保存</button> "
                f"<button type='button' class='btn btn-ghost' onclick=\"toggleEdit({sid})\">取消</button>"
                f"</div>"
                f"</form></td></tr>"
            )
    rows_html = "\n".join(rows_parts)

    table_html = f"""
    <div class="table-wrap">
    <table>
        <thead>
            <tr><th>策略名称</th><th>聊天室配置</th><th>限额</th><th>使用设备数</th><th>操作</th></tr>
        </thead>
        <tbody>{rows_html}</tbody>
    </table>
    </div>"""

    create_form = f"""
    <form method="POST" action="/admin/strategy/create">
        {org_hidden}
        <div class="form-row">
            <div class="form-group">
                <label>策略名称</label>
                <input type="text" name="name" placeholder="例如：客服值班策略" required>
            </div>
            <div class="form-group">
                <label>并发 LLM</label>
                <input type="number" name="max_concurrent_llm" value="2" min="1" required>
            </div>
            <div class="form-group">
                <label>60s 调用上限</label>
                <input type="number" name="max_calls_per_min" value="5" min="1" required>
            </div>
        </div>
        <table style="margin-top:10px">
            <thead><tr><th>聊天室</th><th>模式</th><th>合并窗口(秒)</th><th>追踪时长(秒)</th></tr></thead>
            <tbody>{_room_matrix_rows({})}</tbody>
        </table>
        <div style="margin-top:10px">
            <button type="submit" class="btn btn-primary">➕ 新建策略</button>
        </div>
    </form>"""

    note_html = """
    <div class="note note-warn">
        <b>说明：</b>修改策略后，绑定该策略的所有设备<b>立即生效</b>（引用关系）。删除策略时，使用中的设备将自动回退到默认策略。<br>
        默认策略不可修改、不可删除；新注册设备默认使用默认策略。
    </div>"""

    content = f"""
    <div class="card">
        <h3>🔄 回复策略组管理</h3>
        <p class="card-sub">四类聊天室（外部群 / 外部私聊 / 内部群 / 内部私聊）各自配置回复模式与合并窗口（入队消息聚合窗口，秒）：群聊支持 追踪@（被@后持续追踪该话题并回复）、仅@回复、全部回复、不回复；私聊支持 回复 / 不回复。另有设备级限额（同时最大 LLM 调用数 + 60s 内最大调用数）。</p>
        {note_html}
    </div>
    <div class="card">
        <h3>➕ 新建回复策略</h3>
        {create_form}
    </div>
    <div class="card">
        <h3>📋 策略列表</h3>
        {table_html}
    </div>
    <script>
    function toggleEdit(id) {{
        var el = document.getElementById('edit-' + id);
        if (el) el.style.display = (el.style.display === 'none') ? '' : 'none';
    }}
    </script>"""

    return page(
        "回复策略", "🔄 回复策略组", content, msg, active="/admin/strategies",
        org_id=org_id, org_name=cur_org_name, org_active="strategies",
    )


@router.post("/admin/strategy/create")
async def admin_strategy_create(
    name: str = Form(...),
    mode_1: str = Form(...),
    merge_window_1: int = Form(...),
    track_duration_1: int = Form(180),
    mode_2: str = Form(...),
    merge_window_2: int = Form(...),
    mode_3: str = Form(...),
    merge_window_3: int = Form(...),
    track_duration_3: int = Form(180),
    mode_4: str = Form(...),
    merge_window_4: int = Form(...),
    max_concurrent_llm: int = Form(2),
    max_calls_per_min: int = Form(5),
    org_id: int = Query(0),
):
    base = "/admin/strategies"
    try:
        config = _config_from_form({
            "mode_1": mode_1, "merge_window_1": merge_window_1, "track_duration_1": track_duration_1,
            "mode_2": mode_2, "merge_window_2": merge_window_2,
            "mode_3": mode_3, "merge_window_3": merge_window_3, "track_duration_3": track_duration_3,
            "mode_4": mode_4, "merge_window_4": merge_window_4,
        })
        await robot_service.create_strategy(
            name, config,
            max_concurrent_llm=max_concurrent_llm,
            max_calls_per_min=max_calls_per_min,
        )
        return RedirectResponse(url=_redirect_url(base, org_id, f"✅ 策略已创建"), status_code=303)
    except ValueError as e:
        return RedirectResponse(url=_redirect_url(base, org_id, f"❌ {esc(str(e))}"), status_code=303)
    except Exception as e:
        logger.error(f"create strategy failed: {e}")
        return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 创建失败: {esc(str(e))}"), status_code=303)


@router.post("/admin/strategy/{strategy_id}/update")
async def admin_strategy_update(
    strategy_id: int,
    name: str = Form(...),
    mode_1: str = Form(...),
    merge_window_1: int = Form(...),
    track_duration_1: int = Form(180),
    mode_2: str = Form(...),
    merge_window_2: int = Form(...),
    mode_3: str = Form(...),
    merge_window_3: int = Form(...),
    track_duration_3: int = Form(180),
    mode_4: str = Form(...),
    merge_window_4: int = Form(...),
    max_concurrent_llm: int = Form(2),
    max_calls_per_min: int = Form(5),
    org_id: int = Query(0),
):
    base = "/admin/strategies"
    try:
        config = _config_from_form({
            "mode_1": mode_1, "merge_window_1": merge_window_1, "track_duration_1": track_duration_1,
            "mode_2": mode_2, "merge_window_2": merge_window_2,
            "mode_3": mode_3, "merge_window_3": merge_window_3, "track_duration_3": track_duration_3,
            "mode_4": mode_4, "merge_window_4": merge_window_4,
        })
        success = await robot_service.update_strategy(
            strategy_id, name, config,
            max_concurrent_llm=max_concurrent_llm,
            max_calls_per_min=max_calls_per_min,
        )
        if not success:
            return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 策略不存在"), status_code=303)
        return RedirectResponse(
            url=_redirect_url(base, org_id, f"✅ 策略已更新，绑定设备自动生效"), status_code=303
        )
    except ValueError as e:
        return RedirectResponse(url=_redirect_url(base, org_id, f"❌ {esc(str(e))}"), status_code=303)
    except Exception as e:
        logger.error(f"update strategy failed: {e}")
        return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 更新失败: {esc(str(e))}"), status_code=303)


@router.get("/admin/strategy/{strategy_id}/delete")
async def admin_strategy_delete(strategy_id: int, org_id: int = Query(0)):
    base = "/admin/strategies"
    try:
        success, affected = await robot_service.delete_strategy(strategy_id)
        if not success:
            return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 策略不存在"), status_code=303)
        msg = "✅ 策略已删除"
        if affected:
            msg += f"，{affected} 台设备已回退到默认策略"
        return RedirectResponse(url=_redirect_url(base, org_id, f"{esc(msg)}"), status_code=303)
    except ValueError as e:
        return RedirectResponse(url=_redirect_url(base, org_id, f"❌ {esc(str(e))}"), status_code=303)
    except Exception as e:
        logger.error(f"delete strategy failed: {e}")
        return RedirectResponse(url=_redirect_url(base, org_id, f"❌ 删除失败: {esc(str(e))}"), status_code=303)


@router.post("/api/robot/register")
async def admin_register(
    device_name: str = Form(default=""),
    remark: str = Form(default=""),
    org_id: int = Form(0),
):
    """Register a new robot (form POST from admin page).

    若在组织上下文（org_id>0）内注册，自动把新设备加入该组织，并跳回组织设备页。
    """
    try:
        robot_id, _token = await robot_service.register(
            device_name=device_name, remark=remark
        )
        if org_id:
            await group_service.add_group_org_member(org_id, "device", robot_id)
        base = "/admin"
        return RedirectResponse(url=_redirect_url(base, org_id, f"✅ 设备已注册"), status_code=303)
    except Exception as e:
        logger.error(f"Register failed: {e}")
        base = "/admin"
        return RedirectResponse(url=_redirect_url(base, org_id, f"❌ {esc(str(e))}"), status_code=303)


@router.get("/admin/ban/{robot_id}")
async def admin_ban(robot_id: str, org_id: int = Query(0)):
    await robot_service.ban_robot(robot_id)
    base = "/admin"
    return RedirectResponse(url=_redirect_url(base, org_id, f"✅ 已封禁"), status_code=303)


@router.get("/admin/unban/{robot_id}")
async def admin_unban(robot_id: str, org_id: int = Query(0)):
    await robot_service.unban_robot(robot_id)
    base = "/admin"
    return RedirectResponse(url=_redirect_url(base, org_id, f"✅ 已解封"), status_code=303)


@router.get("/admin/delete/{robot_id}")
async def admin_delete(robot_id: str, org_id: int = Query(0)):
    await robot_service.delete_robot(robot_id)
    base = "/admin"
    return RedirectResponse(url=_redirect_url(base, org_id, f"✅ 设备已删除"), status_code=303)


@router.get("/admin/audit", response_class=HTMLResponse)
async def admin_audit(msg: str = "", org_id: int = 0, limit: int = 50):
    """安全日志：工具调用审计（全量留痕，无人场景追溯底线）"""
    limit = min(max(limit, 10), 200)
    logs = await agent_service.list_audit_logs(limit=limit)
    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if grp:
            cur_org_name = grp["name"]

    role_badge = {
        "customer": '<span class="badge badge-blue">客户</span>',
        "test": '<span class="badge badge-offline">测试</span>',
        "system": '<span class="badge badge-purple">系统</span>',
    }
    rows = []
    for lg in logs:
        args_preview = (lg["args"] or "")[:120]
        result_preview = (lg["result"] or "")[:120]
        rows.append(
            f"<tr>"
            f"<td><code>#{lg['id']}</code></td>"
            f"<td>#{lg['agent_id']}</td>"
            f"<td>{role_badge.get(lg['requester_role'], esc(lg['requester_role']))}</td>"
            f"<td><code>{esc(lg['tool'])}</code></td>"
            f"<td style='font-family:var(--mono);font-size:12px'>{esc(args_preview)}</td>"
            f"<td style='font-family:var(--mono);font-size:12px;color:#475569'>{esc(result_preview)}</td>"
            f"<td>{esc(lg['robot_id'])}<br><span class='muted'>{esc(lg['room_name'])}</span></td>"
            f"<td>{_fmt_dt(lg['created_at'])}</td>"
            f"</tr>"
        )
    rows_html = (
        "\n".join(rows)
        if rows
        else '<tr><td colspan="8" class="empty">暂无工具调用记录（智能体调用工具后自动落库）</td></tr>'
    )

    content = f"""
    <div class="card">
        <h3>🛡️ 工具调用审计（最近 {len(logs)} 条）</h3>
        <p class="card-sub">智能体每次工具调用（联网/表格/工作流/设备指令/建任务）都会在此留痕，用于无人场景下的事后追溯。
        角色来源：客户=企微会话消息；测试=智能体测试运行；系统=无会话上下文的调用。</p>
        <div class="table-wrap">
        <table>
            <thead><tr><th>ID</th><th>智能体</th><th>来源</th><th>工具</th><th>参数</th><th>结果</th><th>设备/会话</th><th>时间</th></tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
        </div>
    </div>
    """
    return page(
        "安全日志",
        "🛡️ 安全日志",
        content,
        msg,
        subtitle="工具调用全量留痕",
        active="audit",
        org_id=org_id,
        org_name=cur_org_name,
        org_active="audit",
    )
