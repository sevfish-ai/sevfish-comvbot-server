"""
组织管理 routes — 统一「公司组织」

一个公司一个组织：组织内可同时包含 设备 / 智能体 / 知识库 / 表格 / 工作流。
数据存 org_groups + org_group_members（member_type 区分成员类型）。

入口流程：组织选择页（/admin/orgs，无侧边栏）→ 点击组织 → 组织首页（/admin/org/{id}）
组织内页面侧边栏显示组织导航，可来回跳转设备/智能体/知识库/表格/工作流。

同时保留原设备分组 REST API（/api/groups*）兼容旧调用方。
"""

import logging
from typing import List

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..models.group import GroupCreateRequest, GroupMemberRequest
from ..services import (
    agent_service,
    ext_user_service,
    group_service,
    knowledge_service,
    memory_service,
    robot_service,
    table_service,
    task_service,
    workflow_service,
)
from ..layout import page, esc

logger = logging.getLogger(__name__)

router = APIRouter(tags=["groups"])


# ════════════════════════════════════════════
#  REST API (JSON) —— 旧设备分组（兼容）
# ════════════════════════════════════════════


@router.post("/api/groups")
async def create_group_api(req: GroupCreateRequest):
    """Create a device group"""
    try:
        group = await group_service.create_group(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "data": group.model_dump()}


@router.get("/api/groups")
async def list_groups_api():
    """List all groups"""
    groups = await group_service.list_groups()
    return {"status": "success", "data": [g.model_dump() for g in groups]}


@router.get("/api/groups/{group_id}")
async def get_group_api(group_id: int):
    """Get group detail with members"""
    group = await group_service.get_group(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")
    return {"status": "success", "data": group.model_dump()}


@router.delete("/api/groups/{group_id}")
async def delete_group_api(group_id: int):
    """Delete a group"""
    ok = await group_service.delete_group(group_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")
    return {"status": "success", "message": f"Group {group_id} deleted"}


@router.post("/api/groups/{group_id}/members")
async def add_member_api(group_id: int, req: GroupMemberRequest):
    """Add a robot to a group"""
    if await robot_service.get_robot(req.robot_id) is None:
        raise HTTPException(status_code=404, detail=f"Robot {req.robot_id} not found")
    ok = await group_service.add_robot(group_id, req.robot_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")
    return {"status": "success", "message": f"Robot {req.robot_id} added to group {group_id}"}


@router.delete("/api/groups/{group_id}/members/{robot_id}")
async def remove_member_api(group_id: int, robot_id: str):
    """Remove a robot from a group"""
    ok = await group_service.remove_robot(group_id, robot_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Group {group_id} not found")
    return {"status": "success", "message": f"Robot {robot_id} removed from group {group_id}"}


# ════════════════════════════════════════════
#  统一「公司分组」页面
# ════════════════════════════════════════════

_MEMBER_TYPES = [
    ("device", "📱", "设备", "/admin"),
    ("agent", "🤖", "智能体", "/admin/agents"),
    ("knowledge", "📚", "知识库", "/admin/knowledge"),
    ("memory", "🧠", "长期记忆", "/admin/memory"),
    ("table", "🗄️", "表格", "/admin/tables"),
    ("workflow", "🔄", "工作流", "/admin/workflows"),
    ("task", "📋", "任务", "/admin/tasks"),
]

# 模块入口卡片的简短说明
_MEMBER_DESC = {
    "device": "企业微信机器人设备，在线状态与收发控制",
    "agent": "LLM 智能体，配置模型、绑定设备自动回复",
    "knowledge": "资料库，为智能体提供 RAG 检索",
    "memory": "客户长期记忆，智能体自己写入 + 人工补充",
    "table": "自定义数据表，授权给智能体读写",
    "workflow": "编排自动化流程，对接设备与智能体",
    "task": "定时/条件触发的自动化任务，执行工作流或下发指令",
}


@router.get("/admin/orgs")
async def admin_orgs(msg: str = ""):
    """组织选择页（无侧边栏）"""
    orgs = await group_service.list_org_groups()

    # 每个组织一张卡片：名称 + 成员数 + 各类型资源概览
    card_parts = []
    for g in orgs:
        # 各类型成员数量
        counts = g.get("member_counts") or {}
        stat_chips = "".join(
            f'<span class="stat-chip">{icon} {label} <b>{counts.get(mtype, 0)}</b></span>'
            for mtype, icon, label, _ in _MEMBER_TYPES
        )
        card_parts.append(
            f'<a href="/admin/org/{g["id"]}" class="org-card">'
            f'<div class="org-card-hd">'
            f'<span class="org-card-icon">🏢</span>'
            f'<div style="flex:1;min-width:0">'
            f'<div class="org-card-name">{esc(g["name"])}</div>'
            f'<div class="org-card-desc">{esc(g["description"]) or "暂无描述"}</div>'
            f'</div>'
            f'<span class="org-card-arrow">→</span>'
            f'</div>'
            f'<div class="inline-flex" style="gap:6px">{stat_chips}</div>'
            f'</a>'
        )
    if not card_parts:
        grid_html = (
            '<div class="empty-card">暂无组织，先创建一个组织吧</div>'
        )
    else:
        grid_html = (
            f'<div class="org-grid">'
            f'{"".join(card_parts)}</div>'
        )

    content = f"""
    <div class="card">
        <h3>🏢 创建组织</h3>
        <p class="card-sub">一个公司一个组织。组织内可同时加入设备、智能体、知识库、表格、工作流，便于按公司组织与管理。</p>
        <form method="POST" action="/admin/org/create" class="form-row">
            <div class="form-group" style="flex:1"><label>组织名称 *（公司/客户名）</label><input type="text" name="name" placeholder="如：甲公司" required></div>
            <div class="form-group" style="flex:2"><label>描述</label><input type="text" name="description" placeholder="备注信息"></div>
            <button class="btn btn-primary">创建组织</button>
        </form>
    </div>
    <div class="card">
        <h3>🤖 AI 配置组织</h3>
        <p class="card-sub">让「组织配置智能体」听你描述需求，直接建好组织与配套的智能体、表格、工作流、知识库、任务、设备和客户账号。它单独配 LLM 与提示词，不属于任何组织。</p>
        <div class="inline-flex" style="gap:8px">
            <a class="btn btn-primary" href="/admin/orgs/provisioner/chat">💬 打开配置台</a>
            <a class="btn" href="/admin/orgs/provisioner" style="background:#f1f2f6;color:#374151;border:1px solid #e5e7eb">🔧 配置智能体</a>
        </div>
    </div>
    <div class="card">
        <h3>📦 选择组织进入</h3>
        {grid_html}
    </div>
    """
    return page("组织选择", "组织选择", content, msg, bare=True)


@router.post("/admin/org/create")
async def admin_org_create(request: Request):
    form = await request.form()
    name = (form.get("name") or "").strip()
    description = form.get("description") or ""
    try:
        gid = await group_service.create_org_group(name, description)
        return RedirectResponse(
            url=f"/admin/org/{gid}?msg=✅ 组织已创建", status_code=303
        )
    except Exception as e:
        logger.error(f"create org failed: {e}")
        return RedirectResponse(
            url=f"/admin/orgs?msg=❌ {esc(str(e))}", status_code=303
        )


@router.get("/admin/org/{org_id}/delete")
async def admin_org_delete(org_id: int):
    try:
        ok = await group_service.delete_org_group(org_id)
        return RedirectResponse(
            url=f"/admin/orgs?msg={'✅ 组织已删除' if ok else '❌ 组织不存在'}",
            status_code=303,
        )
    except Exception as e:
        logger.error(f"delete org failed: {e}")
        return RedirectResponse(
            url=f"/admin/orgs?msg=❌ {esc(str(e))}", status_code=303
        )


@router.get("/admin/org/{org_id}")
async def admin_org_home(org_id: int, msg: str = ""):
    """组织首页：刚进入组织时的落地页，各模块概览 + 入口"""
    org = await group_service.get_org_group(org_id)
    if not org:
        return RedirectResponse(url="/admin/orgs?msg=❌ 组织不存在", status_code=303)
    members = org.get("members") or {}

    # 顶部：组织信息卡
    info_html = f"""
    <div class="card" style="display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap">
        <div class="inline-flex" style="gap:14px">
            <span class="bare-logo-mark" style="width:44px;height:44px;font-size:22px;border-radius:12px">🏢</span>
            <div>
                <h3 style="margin-bottom:2px">{esc(org['name'])} <span class="muted" style="font-size:12px">#{org['id']}</span></h3>
                <p class="card-sub mb-0">{esc(org['description']) or '暂无描述'}</p>
            </div>
        </div>
        <div class="inline-flex" style="gap:8px">
            <a href="/admin/org/{org_id}/members" class="btn btn-outline btn-sm">👥 成员与外网账号</a>
            <a href="/admin/orgs" class="btn btn-ghost btn-sm">⬅ 组织选择</a>
            <a href="/admin/org/{org_id}/delete" class="btn btn-danger btn-sm"
               onclick="return confirm('删除组织「{esc(org['name'])}」？组内成员关系将一并清除。')">删除组织</a>
        </div>
    </div>"""

    # 5 个模块入口卡片：资源数量 + 进入按钮（成员增删在各模块页内完成）
    module_cards = []
    for mtype, icon, label, mgmt_url in _MEMBER_TYPES:
        cur_ids = set(members.get(mtype, []))
        module_cards.append(
            f"""
        <a href="{mgmt_url}?org_id={org_id}" class="module-card">
            <div class="module-card-hd">
                <span class="module-card-title">{icon} {label}</span>
                <span class="module-card-count">{len(cur_ids)} 个</span>
            </div>
            <p class="module-card-desc mb-0">{_MEMBER_DESC.get(mtype, '')}</p>
            <span class="btn btn-primary btn-sm">进入 →</span>
        </a>"""
        )
    modules_html = (
        f'<div class="module-grid">'
        f'{"".join(module_cards)}</div>'
    )

    content = f"""
    {info_html}
    <div class="card">
        <h3>🧩 模块入口</h3>
        {modules_html}
    </div>
    """
    return page(
        f"组织 {org['name']}", f"🏢 {esc(org['name'])}", content, msg,
        org_id=org_id, org_name=org["name"], org_active="home",
    )


# ════════════════════════════════════════════
#  组织成员 + 外网客户账号（一页管理）
# ════════════════════════════════════════════

# 走 org_group_members 关联表的成员类型（task 例外：归属存在 tasks.org_id 列）
_LINKABLE = [t for t in _MEMBER_TYPES if t[0] != "task"]
_LINKABLE_KEYS = {t[0] for t in _LINKABLE}

# 客户账号可配置的可见范围类别（与 /ext 的 _SCOPE_KINDS 一致）
# 页面渲染与保存白名单共用这个元组，免得出现"页面勾得上、保存被忽略"。
# memory（长期记忆）：/ext 有记忆页面，且**唯一一个客户可写**的资源（只能管理"记忆"本身，
# 不能建/删记忆集）。加类别时记得同步改 app/routes/ext.py 的 _SCOPE_KINDS。
_SCOPE_KINDS = ("device", "table", "agent", "knowledge", "memory")
_KIND_LABEL = {"device": "设备", "table": "表格", "agent": "智能体", "knowledge": "知识库", "memory": "长期记忆"}


async def _member_options(mtype: str):
    """[(member_id, 显示名)] —— 该类型的全部资源（用于"添加"下拉框与名称回显）。"""
    if mtype == "device":
        rows = await robot_service.list_robots()
        return [(r.robot_id, f"{r.device_name or r.robot_id}（{r.robot_id}）") for r in rows]
    if mtype == "agent":
        return [(str(a.id), a.name) for a in await agent_service.list_agents()]
    if mtype == "knowledge":
        return [(str(k.id), k.name) for k in await knowledge_service.list_kbs()]
    if mtype == "memory":
        return [(str(m["id"]), m["name"]) for m in await memory_service.list_sets()]
    if mtype == "table":
        return [(str(t.id), t.name) for t in await table_service.list_tables()]
    if mtype == "workflow":
        return [(str(w.id), w.name) for w in await workflow_service.list_workflows()]
    return []


@router.get("/admin/org/{org_id}/members")
async def admin_org_members(org_id: int, msg: str = ""):
    """组织成员 + 该组织的外网客户账号，一页搞定。

    成员 = 这个组织能看到/管理哪些资源；对外接口（/ext）的可见范围完全由这里决定。
    """
    org = await group_service.get_org_group(org_id)
    if not org:
        return RedirectResponse(url="/admin/orgs?msg=❌ 组织不存在", status_code=303)

    blocks = []
    for mtype, icon, label, _url in _LINKABLE:
        cur_ids = [str(x) for x in await group_service.get_group_org_members(org_id, mtype)]
        options = await _member_options(mtype)
        name_of = {mid: name for mid, name in options}

        # 成员只展示、不移除：移出组织本质是一次删除操作，不该出现在这个页面上
        chips = "".join(
            f'<span class="chip-check">{esc(name_of.get(mid, mid))}</span>' for mid in cur_ids
        ) or '<span class="muted">（暂无）</span>'

        addable = "".join(
            f'<option value="{esc(mid)}">{esc(name)}</option>'
            for mid, name in options
            if mid not in set(cur_ids)
        )
        add_form = (
            f"""<form method="POST" action="/admin/org/{org_id}/members/add" class="form-row" style="margin-top:10px">
                <input type="hidden" name="member_type" value="{mtype}">
                <div class="form-group" style="flex:1"><select name="member_id" class="inp-sm">{addable}</select></div>
                <button class="btn btn-outline btn-sm">加入组织</button>
            </form>"""
            if addable
            else '<p class="form-hint" style="margin-top:8px">该类型的资源已全部在本组织中</p>'
        )
        blocks.append(
            f"""
        <div style="padding:12px 0;border-top:1px solid var(--line,#e5e5e5)">
            <div class="inline-flex" style="gap:8px;align-items:center;flex-wrap:wrap">
                <b>{icon} {label}</b>
                <span class="badge badge-blue">{len(cur_ids)}</span>
            </div>
            <div class="inline-flex" style="gap:6px;flex-wrap:wrap;margin-top:8px">{chips}</div>
            {add_form}
        </div>"""
        )

    # 任务例外：归属在 tasks.org_id 列，不在关联表里，这里只读展示
    task_rows = await task_service.list_tasks(org_id=org_id)
    task_html = "".join(
        f'<span class="chip-check">{esc(t.get("name") or t["id"])}</span>' for t in task_rows
    ) or '<span class="muted">（暂无）</span>'
    blocks.append(
        f"""
        <div style="padding:12px 0;border-top:1px solid var(--line,#e5e5e5)">
            <div class="inline-flex" style="gap:8px;align-items:center;flex-wrap:wrap">
                <b>📋 任务</b><span class="badge badge-blue">{len(task_rows)}</span>
            </div>
            <div class="inline-flex" style="gap:6px;flex-wrap:wrap;margin-top:8px">{task_html}</div>
            <p class="form-hint" style="margin-top:8px">任务的归属在创建任务时设定，请到
                <a href="/admin/tasks?org_id={org_id}">任务页</a>调整。</p>
        </div>"""
    )

    # ── 外网客户账号 ──
    users = await ext_user_service.list_users(org_id=org_id)
    # 可见范围摘要：未配置 = 跟随组织（全可见）
    scope_summary = {}
    for u in users:
        us = await ext_user_service.get_scope(u["id"])
        scope_summary[u["id"]] = (
            '<span class="badge badge-blue">跟随组织</span>'
            if not us
            else " ".join(f"{_KIND_LABEL.get(k, k)} {len(v)}" for k, v in sorted(us.items()))
            or '<span class="badge badge-warn">全部不可见</span>'
        )
    acc_rows = "".join(
        f"""<tr>
            <td>{esc(u["username"])}</td>
            <td>{'✅ 启用' if u["enabled"] else '🚫 停用'}</td>
            <td>{esc(u["last_login"] or "-")}</td>
            <td>{scope_summary.get(u["id"], "")}</td>
            <td class="inline-flex" style="gap:6px;flex-wrap:wrap">
                <a class="btn btn-outline btn-sm" href="/admin/org/{org_id}/scope/{u["id"]}">可见范围</a>
                <form method="POST" action="/admin/org/{org_id}/ext_user/passwd" class="inline-flex" style="gap:4px">
                    <input type="hidden" name="username" value="{esc(u["username"])}">
                    <input type="password" name="password" placeholder="新密码" class="inp-sm" required>
                    <button class="btn btn-ghost btn-sm">改密</button>
                </form>
                <a class="btn btn-ghost btn-sm"
                   href="/admin/org/{org_id}/ext_user/toggle?username={esc(u["username"])}&on={0 if u["enabled"] else 1}">
                   {'停用' if u["enabled"] else '启用'}</a>
                <a class="btn btn-danger btn-sm" onclick="return confirm('删除账号 {esc(u["username"])}？')"
                   href="/admin/org/{org_id}/ext_user/delete?username={esc(u["username"])}">删除</a>
            </td>
        </tr>"""
        for u in users
    ) or '<tr><td colspan="5" class="empty">该组织还没有外网账号</td></tr>'

    content = f"""
    <div class="card">
        <h3>👥 组织成员</h3>
        <p class="card-sub">组织成员决定这个组织「拥有」哪些设备 / 智能体 / 知识库 / 表格 / 工作流。
        这里只做<b>加入</b>——移出资源等同于删除，请到该资源自己的页面处理。<br>
        至于"哪个客户账号能看其中哪些"，用下方账号的「可见范围」逐个配置（不配 = 跟随组织全可见）。</p>
        {''.join(blocks)}
    </div>

    <div class="card">
        <h3>🌐 外网客户账号</h3>
        <p class="card-sub">这些账号用于外网客户登录 <code>/ext</code>，每个账号绑定到本组织。
        点「可见范围」可逐账号限定能看到哪些设备 / 表格 / 智能体 / 知识库。
        需要先在 <code>.env</code> 配置 <code>EXT_TUNNEL_SECRET</code>（或本地预览的 <code>EXT_LOCAL_PREVIEW=true</code>）才会对外开放。</p>
        <div class="tbl-scroll">
        <table>
            <tr><th>账号</th><th>状态</th><th>末次登录</th><th>可见范围</th><th>操作</th></tr>
            {acc_rows}
        </table>
        </div>
        <form method="POST" action="/admin/org/{org_id}/ext_user/add" class="form-row" style="margin-top:14px">
            <div class="form-group" style="flex:1"><label>新账号</label>
                <input type="text" name="username" placeholder="如：客户甲" required></div>
            <div class="form-group" style="flex:1"><label>密码（≥8 位）</label>
                <input type="password" name="password" minlength="8" required></div>
            <button class="btn btn-primary">创建账号</button>
        </form>
    </div>
    """
    return page(
        f"{org['name']} 成员", f"👥 {esc(org['name'])} · 成员与外网账号", content, msg,
        org_id=org_id, org_name=org["name"], org_active="members",
    )


@router.post("/admin/org/{org_id}/members/add")
async def admin_org_member_add(
    org_id: int, member_type: str = Form(...), member_id: str = Form("")
):
    if member_type not in _LINKABLE_KEYS or not member_id.strip():
        return RedirectResponse(
            url=f"/admin/org/{org_id}/members?msg=❌ 参数不合法", status_code=303
        )
    await group_service.add_group_org_member(org_id, member_type, member_id.strip())
    logger.info(f"组织 {org_id} 加入成员 {member_type}/{member_id}")
    return RedirectResponse(
        url=f"/admin/org/{org_id}/members?msg=✅ 已加入组织", status_code=303
    )


@router.post("/admin/org/{org_id}/ext_user/add")
async def admin_org_ext_user_add(
    org_id: int, username: str = Form(""), password: str = Form("")
):
    try:
        await ext_user_service.add_user(username.strip(), password, org_id)
        logger.info(f"组织 {org_id} 新建外网账号 {username}")
        return RedirectResponse(
            url=f"/admin/org/{org_id}/members?msg=✅ 账号已创建", status_code=303
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/admin/org/{org_id}/members?msg=❌ {esc(str(e))}", status_code=303
        )


@router.post("/admin/org/{org_id}/ext_user/passwd")
async def admin_org_ext_user_passwd(
    org_id: int, username: str = Form(""), password: str = Form("")
):
    if len(password) < 8:
        return RedirectResponse(
            url=f"/admin/org/{org_id}/members?msg=❌ 密码至少 8 位", status_code=303
        )
    ok = await ext_user_service.set_password(username.strip(), password)
    return RedirectResponse(
        url=f"/admin/org/{org_id}/members?msg={'✅ 密码已修改' if ok else '❌ 账号不存在'}",
        status_code=303,
    )


@router.get("/admin/org/{org_id}/ext_user/toggle")
async def admin_org_ext_user_toggle(org_id: int, username: str, on: int = 1):
    await ext_user_service.set_enabled(username.strip(), bool(on))
    return RedirectResponse(
        url=f"/admin/org/{org_id}/members?msg={'✅ 已启用' if on else '🚫 已停用'}", status_code=303
    )


@router.get("/admin/org/{org_id}/ext_user/delete")
async def admin_org_ext_user_delete(org_id: int, username: str):
    await ext_user_service.delete_user(username.strip())
    # 该账号若正在登录，其会话下个请求就会失效（每请求都查账号有效性）
    return RedirectResponse(
        url=f"/admin/org/{org_id}/members?msg=✅ 账号已删除", status_code=303
    )


@router.get("/admin/org/{org_id}/scope/{user_id}")
async def admin_org_user_scope(org_id: int, user_id: int, msg: str = ""):
    """单个外网账号的可见范围：勾选它能在 /ext 看到哪些资源。

    不配 = 跟随组织（全可见）；一旦配过，就完全以这里为准，没勾的类别整类看不见。
    """
    org = await group_service.get_org_group(org_id)
    if not org:
        return RedirectResponse(url="/admin/orgs?msg=❌ 组织不存在", status_code=303)
    user = await ext_user_service.get(user_id)
    if not user or int(user["org_id"]) != int(org_id):
        return RedirectResponse(
            url=f"/admin/org/{org_id}/members?msg=❌ 账号不存在或不属于本组织", status_code=303
        )

    configured = await ext_user_service.count_scope(user_id) > 0
    cur = await ext_user_service.get_scope(user_id)

    blocks = []
    for mtype, icon, label, _u in _LINKABLE:
        if mtype not in _SCOPE_KINDS:
            continue
        org_ids = [str(x) for x in await group_service.get_group_org_members(org_id, mtype)]
        options = await _member_options(mtype)
        name_of = {mid: name for mid, name in options}
        # 未配置过 → 默认全选（等价于当前行为）；配置过 → 按已存范围勾选
        checked = set(org_ids) if not configured else set(cur.get(mtype, []))
        items = "".join(
            f"""<label style="display:flex;align-items:center;gap:8px;padding:5px 0;cursor:pointer;width:fit-content">
                <input class="check" type="checkbox" name="{mtype}" value="{esc(mid)}"{' checked' if mid in checked else ''}>
                <span>{esc(name_of.get(mid, mid))}</span>
            </label>"""
            for mid in org_ids
        ) or '<p class="form-hint">本组织内该类别暂无资源</p>'
        blocks.append(
            f"""
        <div style="padding:12px 0;border-top:1px solid var(--line,#e5e5e5)">
            <div class="inline-flex" style="gap:8px;align-items:center">
                <b>{icon} {label}</b>
                <span class="badge badge-blue">{len(org_ids)} 个可选</span>
            </div>
            <div style="margin-top:6px">{items}</div>
        </div>"""
        )

    state_hint = (
        '<span class="badge badge-blue">跟随组织（未限定）</span>'
        if not configured
        else '<span class="badge badge-warn">已限定</span>'
    )
    content = f"""
    <div class="card">
        <h3>👁 {esc(user["username"])} 的可见范围 {state_hint}</h3>
        <p class="card-sub">
            勾选这个账号登录 <code>/ext</code> 后能看到的资源。<b>不勾就是看不见</b>。<br>
            没配置过时默认跟随组织（全部可见）；一旦保存过，就完全以本页面为准 —
            某个类别一个都没勾，该类别对该账号整类隐藏。
        </p>
        <form method="POST" action="/admin/org/{org_id}/scope/{user_id}">
            {''.join(blocks)}
            <div class="inline-flex" style="gap:8px;margin-top:16px;flex-wrap:wrap">
                <button class="btn btn-primary">保存可见范围</button>
                <a class="btn btn-ghost" href="/admin/org/{org_id}/members">返回成员页</a>
            </div>
            <p class="form-hint" style="margin-top:10px">
                一项都不勾无法保存（那等于把该账号废掉）。要彻底收回权限，请回到成员页把账号「停用」或删除。
            </p>
        </form>
    </div>
    """
    return page(
        f"{user['username']} 可见范围", f"👁 {esc(user['username'])} · 可见范围", content, msg,
        org_id=org_id, org_name=org["name"], org_active="members",
    )


@router.post("/admin/org/{org_id}/scope/{user_id}")
async def admin_org_user_scope_save(org_id: int, user_id: int, request: Request):
    """保存可见范围。

    种类**按 `_SCOPE_KINDS` 动态读取**（`form.getlist(kind)`），不要再写成 device/table/agent/
    knowledge 四个写死的 Form 参数 —— 那样加类别时会出现"页面渲染了新类别、保存却悄悄忽略它"
    （记忆集就踩过：勾了保存后范围是空的）。
    """
    user = await ext_user_service.get(user_id)
    if not user or int(user["org_id"]) != int(org_id):
        return RedirectResponse(
            url=f"/admin/org/{org_id}/members?msg=❌ 账号不存在或不属于本组织", status_code=303
        )
    form = await request.form()
    mapping = {kind: [str(x) for x in form.getlist(kind)] for kind in _SCOPE_KINDS}
    if not any(mapping.values()):
        return RedirectResponse(
            url=f"/admin/org/{org_id}/scope/{user_id}?msg=❌ 至少要勾选一项；要收回全部权限请停用账号",
            status_code=303,
        )
    # 只保留仍在本组织内的项，避免页面被改后写进越界范围
    for kind, ids in mapping.items():
        allowed = {str(x) for x in await group_service.get_group_org_members(org_id, kind)}
        mapping[kind] = [i for i in dict.fromkeys(ids) if str(i) in allowed]
    await ext_user_service.set_scope(user_id, mapping)
    logger.info(f"账号 {user_id} 可见范围更新 by 组织 {org_id}: {mapping}")
    return RedirectResponse(
        url=f"/admin/org/{org_id}/scope/{user_id}?msg=✅ 可见范围已保存，立即生效", status_code=303
    )
