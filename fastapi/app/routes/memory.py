"""
长期记忆（Memory）管理页：记忆集 / 客户 / 事实的查看与人工维护。

人工维护是记忆的补充与前期准备：
- 组织创立之初，先建记忆集 → 建客户 → 批量粘贴事实，智能体即可「直接上手」；
- 智能体自己写入的事实（source=agent）同样出现在这里，可人工修正、合并重复客户。
记忆是「只追加的事实行」，人加的一行与智能体加的一行在数据上等价，不存在"改坏结构"。
"""

import logging
from typing import List

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from ..layout import esc, page
from ..services import agent_service, memory_service, group_service, robot_service
from ..services.memory_service import (
    CLEANUP_FREQ_CHOICES,
    cleanup_freq_label,
    ITEM_MAX_CHARS,
    ITEMS_PER_CUSTOMER,
    LIMITS,
)
from ..services.memory_cleanup_service import memory_cleanup_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["memory"])


def _base(org_id: int = 0, view: int = 0, extra: str = "") -> str:
    q = []
    if view:
        q.append(f"view={view}")
    if org_id:
        q.append(f"org_id={org_id}")
    if extra:
        q.append(extra)
    return "/admin/memory" + (("?" + "&".join(q)) if q else "")


def _back(url: str, msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"{url}{'&' if '?' in url else '?'}msg={msg}", status_code=303)


def _set_form(action: str, org_id: int, st: dict = None, agents: List = None) -> str:
    st = st or {}
    agents = agents or []
    oid = f'<input type="hidden" name="org_id" value="{org_id}">' if org_id else ""
    # 没配过 = 不清理（0），不再用一个单独的开关
    cur_hours = int(st.get("cleanup_hours") or 0)   # 频率预设见 memory_service.CLEANUP_FREQ_CHOICES
    if not st.get("id"):
        cur_hours = 0
    cur_agent = int(st.get("cleanup_agent_id") or 0)
    freq_opts = "".join(
        f'<option value="{h}"{" selected" if h == cur_hours else ""}>{label}'
        f'{"" if not h else f"（{h} 小时）"}</option>'
        for h, label in CLEANUP_FREQ_CHOICES
    )
    if cur_hours not in [h for h, _ in CLEANUP_FREQ_CHOICES] and cur_hours > 0:
        freq_opts += f'<option value="{cur_hours}" selected>自定义（{cur_hours} 小时）</option>'
    agent_opts = '<option value="0">— 请选择 —</option>' + "".join(
        f'<option value="{a.id}"{" selected" if int(a.id) == cur_agent else ""}>{esc(a.name)}（#{a.id}）</option>'
        for a in agents
    )
    return f"""
    <form method="POST" action="{action}">
      <div class="form-row" style="align-items:flex-start">
        {oid}
        <div class="form-group" style="flex:1"><label>记忆集名称 *</label>
            <input type="text" name="name" required placeholder="如：售前客户记忆 / 售后客户记忆" value="{esc(st.get('name', ''))}"></div>
        <div class="form-group" style="flex:2"><label>描述</label>
            <input type="text" name="description" placeholder="这套记忆记什么、给哪些设备用" value="{esc(st.get('description', ''))}"></div>
      </div>
      <div class="form-row" style="align-items:flex-start;margin-top:8px">
        <div class="form-group"><label>记忆清理频率</label>
            <select name="cleanup_hours" id="cleanup-hours-{st.get('id', 'new')}"
                    onchange="(function(s){{var a=document.getElementById('cleanup-agent-{st.get('id', 'new')}');if(a)a.required=(parseInt(s.value,10)>0);}})(this)">
              {freq_opts}</select>
            <span class="muted" style="font-size:12px">只整理智能体写入的事实；人工补录/导入的不动</span></div>
        <div class="form-group" style="flex:1"><label>用哪个智能体清理</label>
            <select name="cleanup_agent_id" id="cleanup-agent-{st.get('id', 'new')}"{' required' if cur_hours > 0 else ''}>{agent_opts}</select>
            <span class="muted" style="font-size:12px">选了频率就必须指定智能体（只列本组织的）</span></div>
      </div>
      <div style="margin-top:8px"><button class="btn btn-primary">{'保存' if st.get('id') else '创建记忆集'}</button></div>
    </form>"""


def _limits_card() -> str:
    """护栏与阈值：数字全部来自 memory_service 的常量，页面不另抄一份。

    **全页只有这一份**（`?view=` 是列表 + 详情同页渲染，页内再放一份就是重复一大块说明）。
    打开某套记忆集时想看该套用量，看上方列表行里的「客户 / 事实」两列即可。
    """
    rows = "".join(
        f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in LIMITS
    )
    return f"""
    <div class="card">
        <h3>🛡️ 护栏与阈值</h3>
        <p class="card-sub">以下阈值定义在 <code>services/memory_service.py</code> 顶部常量，页面直接读取，不存在两套数字。
        超过阈值只会被拒绝或截断，不会静默丢数据。事实行超过 {ITEM_MAX_CHARS} 字会标红提示（注入时截断），
        客户事实数超过 {ITEMS_PER_CUSTOMER} 条会标黄（只注入最近 {ITEMS_PER_CUSTOMER} 条）。</p>
        <div class="table-wrap"><table>
            <thead><tr><th>项</th><th>限制</th></tr></thead>
            <tbody>{rows}</tbody>
        </table></div>
    </div>"""


def _item_rows(
    set_id: int,
    items: List[dict],
    org_id: int,
    view: int,
    edit_item: int,
) -> str:
    if not items:
        return '<tr><td colspan="3" class="empty">暂无记录</td></tr>'
    rows = []
    for it in items:
        over = len(it["content"]) > ITEM_MAX_CHARS
        flag = (
            f'<span class="badge badge-offline">超 {ITEM_MAX_CHARS} 字，注入时截断</span>'
            if over
            else ""
        )
        src = {"agent": "智能体", "import": "导入", "manual": "人工"}.get(it["source"], it["source"])
        meta = f"{src} · {esc(it['updated_at'][:16])}"
        if it["origin"]:
            meta += f" · {esc(it['origin'])}"
        edit_url = _base(org_id, view, f"edit_item={it['id']}")
        del_url = (
            f"/admin/memory/item/{it['id']}/delete?set_id={set_id}&view={view}&org_id={org_id}"
        )
        if edit_item == it["id"]:
            body = f"""
            <form method="POST" action="/admin/memory/{set_id}/item/{it['id']}/update" class="form-row">
                <input type="hidden" name="org_id" value="{org_id}">
                <input type="hidden" name="view" value="{view}">
                <div class="form-group" style="flex:1"><textarea name="content" rows="2" style="width:100%">{esc(it['content'])}</textarea></div>
                <button class="btn btn-primary btn-sm">保存</button>
                <a class="btn btn-ghost btn-sm" href="{_base(org_id, view)}">取消</a>
            </form>"""
        else:
            body = (
                f"{esc(it['content'])} {flag}"
                f'<div class="cell-sub">{meta}</div>'
            )
        rows.append(
            f"<tr><td>{body}</td>"
            f'<td style="white-space:nowrap">'
            + (
                ""
                if edit_item == it["id"]
                else f'<a class="btn btn-ghost btn-sm" href="{edit_url}">编辑</a> '
                f'<a class="btn btn-danger btn-sm" href="{del_url}" onclick="return confirm(\'删除这条记忆？\')">删除</a>'
            )
            + "</td></tr>"
        )
    return "".join(rows)


def _customer_block(
    set_id: int,
    cust: dict,
    items: List[dict],
    others: List[dict],
    org_id: int,
    view: int,
    edit_item: int,
    hidden: int = 0,
    all_items: int = 0,
    q: str = "",
    open_block: bool = False,
) -> str:
    merge_opts = "".join(
        f'<option value="{o["id"]}">{esc(o["name"])}</option>' for o in others
    )
    merge_html = (
        f"""<form method="POST" action="/admin/memory/{set_id}/customer/{cust['id']}/merge" class="inline-flex" style="gap:4px">
            <input type="hidden" name="org_id" value="{org_id}">
            <input type="hidden" name="view" value="{view}">
            <select name="dst_id" class="inp-sm">{merge_opts}</select>
            <button class="btn btn-ghost btn-sm" onclick="return confirm('把这个客户的事实与别名并入所选客户？')">合并到</button>
        </form>"""
        if others
        else ""
    )
    over = (
        f'<span class="badge badge-offline">事实数 {cust["item_count"]}，注入只取最近 {ITEMS_PER_CUSTOMER} 条</span>'
        if cust["item_count"] > ITEMS_PER_CUSTOMER
        else ""
    )
    more = ""
    if hidden:
        link = _base(org_id, view, f"all=1{('&q=' + q) if q else ''}")
        more = (f'<p class="muted" style="margin:6px 0 0">仅显示最近 {len(items)} 条，还有 {hidden} 条较早的记忆 · '
                f'<a href="{link}">显示全部</a></p>')
    return f"""
    <div style="padding:10px 0;border-top:1px solid var(--border,#e4e7ec)">
      <details{' open' if open_block else ''}>
        <summary style="cursor:pointer;user-select:none;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <b>👤 {esc(cust['name'])}</b>
            <span class="badge badge-blue">{cust['item_count']} 条</span>
            {over}
            {f'<span class="muted">别名：{esc(cust["aliases"])}</span>' if cust['aliases'] else ''}
        </summary>
        <div class="inline-flex" style="gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px">
            <details style="display:inline-block">
                <summary class="btn btn-ghost btn-sm" style="cursor:pointer">改名 / 别名</summary>
                <form method="POST" action="/admin/memory/{set_id}/customer/{cust['id']}/update" class="form-row" style="margin-top:8px">
                    <input type="hidden" name="org_id" value="{org_id}">
                    <input type="hidden" name="view" value="{view}">
                    <div class="form-group" style="flex:1"><label>规范名</label><input type="text" name="name" value="{esc(cust['name'])}" required></div>
                    <div class="form-group" style="flex:2"><label>别名（逗号分隔：群里显示名 / 备注名）</label>
                        <input type="text" name="aliases" value="{esc(cust['aliases'])}" placeholder="如：张三-采购,张三(XX公司)"></div>
                    <button class="btn btn-primary btn-sm">保存</button>
                </form>
            </details>
            {merge_html}
            <a class="btn btn-danger btn-sm" href="/admin/memory/customer/{cust['id']}/delete?set_id={set_id}&view={view}&org_id={org_id}"
               onclick="return confirm('删除客户「{esc(cust['name'])}」及其全部事实？')">删除客户</a>
        </div>
        <table style="margin-top:8px">
            <tbody>{_item_rows(set_id, items, org_id, view, edit_item)}</tbody>
        </table>
        <form method="POST" action="/admin/memory/{set_id}/item/create" class="form-row" style="margin-top:6px">
            <input type="hidden" name="org_id" value="{org_id}">
            <input type="hidden" name="view" value="{view}">
            <input type="hidden" name="customer_id" value="{cust['id']}">
            <div class="form-group" style="flex:1"><input type="text" name="content" required
                placeholder="补一条事实，如：2026-08 询问过 A 型号报价，预算 5 万以内"></div>
            <button class="btn btn-outline btn-sm">添加事实</button>
        </form>
        {more}
      </details>
    </div>"""


@router.get("/admin/memory", response_class=HTMLResponse)
async def admin_memory(
    msg: str = "",
    org_id: int = 0,
    view: int = 0,
    edit_item: int = 0,
    q: str = "",
    all: int = 0,
):
    """记忆集列表 + 详情（view=记忆集 id）。

    记忆一多页面会很长：客户区块默认折叠（`<details>`），每个客户默认只渲染最近
    `ITEMS_PER_CUSTOMER` 条（要清旧账点「显示全部」`all=1`），并支持按客户名/别名筛选（`q=`）。
    正在编辑的那条一定渲染出来（哪怕不在最近 N 条里）。
    """
    cur_org_name = ""
    sets = await memory_service.list_sets()
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if not grp:
            return RedirectResponse(url="/admin/orgs?msg=❌ 组织不存在", status_code=303)
        cur_org_name = grp["name"]
        member_ids = {str(x) for x in grp["members"].get("memory", [])}
        sets = [s for s in sets if str(s["id"]) in member_ids]

    # 清理用的智能体：给 org_id 时只列这个组织的（"使用当前组织中的智能体"）
    agents_all = await agent_service.list_agents()
    if org_id:
        org_agent_ids = {str(x) for x in await group_service.get_group_org_members(org_id, "agent")}
        agents_all = [a for a in agents_all if str(a.id) in org_agent_ids]

    usage = await memory_service.get_robots_of_sets([s["id"] for s in sets])
    robot_names = {
        r.robot_id: (r.device_name or r.robot_id) for r in await robot_service.list_robots()
    }

    def _usage_tags(set_id: int) -> str:
        tags = []
        for robot_id, mode in usage.get(set_id, []):
            label = "维护" if mode == "write" else "只读"
            cls = "badge-online" if mode == "write" else "badge-blue"
            tags.append(
                f'<span class="badge {cls}">{esc(robot_names.get(robot_id, robot_id))}·{label}</span>'
            )
        return " ".join(tags) or '<span class="muted">暂无设备使用</span>'

    rows = "".join(
        f"<tr><td><code>#{s['id']}</code></td>"
        f"<td><span class='cell-main'>{esc(s['name'])}</span><span class='cell-sub'>{esc(s['description'])}</span></td>"
        f"<td>{s['customer_count']} 位 / {s['item_count']} 条</td>"
        f"<td>{_usage_tags(s['id'])}</td>"
        f"<td><a class='btn btn-primary btn-sm' href='{_base(org_id, s['id'])}'>打开</a> "
        f"<a class='btn btn-danger btn-sm' href='/admin/memory/set/{s['id']}/delete?org_id={org_id}' "
        f"onclick=\"return confirm('删除记忆集「{esc(s['name'])}」？其客户、事实与设备绑定将一并删除！')\">删除</a></td></tr>"
        for s in sets
    )
    list_html = f"""
    <div class="card">
        <h3>📋 记忆集</h3>
        <p class="card-sub">一套记忆 = 一个记忆集。多台设备（各自绑智能体）指向同一套＝共享记忆；各指各的＝各自维护一套。
        记忆绑定在「设备」页逐台设置（维护集单选、只读集多选）；组织归属在「成员」页加入。
        记忆跟着设备走：换智能体不换记忆，设备解绑智能体后记忆自动不再注入。</p>
        <div class="table-wrap"><table>
            <thead><tr><th>ID</th><th>名称</th><th>客户 / 事实</th><th>使用中的设备</th><th>操作</th></tr></thead>
            <tbody>{rows or '<tr><td colspan="5" class="empty">暂无记忆集，先在上方创建</td></tr>'}</tbody>
        </table></div>
    </div>"""

    detail_html = ""
    if view:
        st = await memory_service.get_set(view)
        if not st:
            msg = msg or "❌ 记忆集不存在（可能已被删除）"
        else:
            customers = await memory_service.list_customers(view)
            items = await memory_service.list_items(view)
            general = [i for i in items if not i["customer_id"]]
            by_customer = {c["id"]: [] for c in customers}
            for it in items:
                if it["customer_id"]:
                    by_customer.setdefault(it["customer_id"], []).append(it)
            q = (q or "").strip()
            shown_customers = [
                c for c in customers
                if not q or q in (c["name"] or "") or q in (c["aliases"] or "")
            ]

            def _slice(rows: List[dict]) -> tuple:
                """按显示上限切片，保证 edit_item 在可见范围内。返回 (可见, 隐藏条数)。"""
                if all or len(rows) <= ITEMS_PER_CUSTOMER:
                    return rows, 0
                shown = rows[:ITEMS_PER_CUSTOMER]
                if edit_item and not any(int(i["id"]) == int(edit_item) for i in shown):
                    extra = next((i for i in rows if int(i["id"]) == int(edit_item)), None)
                    if extra:
                        shown = shown + [extra]
                return shown, len(rows) - len(shown)

            parts = []
            for c in shown_customers:
                rows = by_customer.get(c["id"], [])
                shown_rows, hidden_rows = _slice(rows)
                parts.append(
                    _customer_block(
                        view,
                        c,
                        shown_rows,
                        [o for o in customers if o["id"] != c["id"]],
                        org_id,
                        view,
                        edit_item,
                        hidden=hidden_rows,
                        all_items=all,
                        q=q,
                        open_block=bool(edit_item) and any(
                            int(i["id"]) == int(edit_item) for i in rows
                        ),
                    )
                )
            blocks = "".join(parts) or ('<p class="muted">没有匹配的客户（换个关键词试试）</p>' if q else
                  '<p class="muted">还没有客户，先添加一个（客户是记忆的基础单位，同一客户跨群仍是同一行）</p>')

            general_shown, general_hidden = _slice(general)
            general_more = ""
            if general_hidden:
                link = _base(org_id, view, f"all=1{('&q=' + q) if q else ''}")
                general_more = (
                    f'<p class="muted" style="margin:6px 0 0">仅显示最近 {len(general_shown)} 条，'
                    f'还有 {general_hidden} 条 · <a href="{link}">显示全部</a></p>'
                )
            general_rows = "".join(
                f"<tr><td>{esc(i['content'])} <span class='badge badge-blue'>通用</span>"
                f"<div class='cell-sub'>{esc(i['updated_at'][:16])}</div></td>"
                f"<td style='white-space:nowrap'>"
                f"<a class='btn btn-ghost btn-sm' href='{_base(org_id, view, f'edit_item={i['id']}')}'>编辑</a> "
                f"<a class='btn btn-danger btn-sm' href='/admin/memory/item/{i['id']}/delete?set_id={view}&view={view}&org_id={org_id}' "
                f"onclick=\"return confirm('删除这条通用记忆？')\">删除</a></td></tr>"
                if edit_item != i["id"]
                else f"<tr><td colspan='2'><form method='POST' action='/admin/memory/{view}/item/{i['id']}/update' class='form-row'>"
                f"<input type='hidden' name='org_id' value='{org_id}'><input type='hidden' name='view' value='{view}'>"
                f"<div class='form-group' style='flex:1'><textarea name='content' rows='2' style='width:100%'>{esc(i['content'])}</textarea></div>"
                f"<button class='btn btn-primary btn-sm'>保存</button>"
                f"<a class='btn btn-ghost btn-sm' href='{_base(org_id, view)}'>取消</a></form></td></tr>"
                for i in general_shown
            ) or '<tr><td colspan="2" class="empty">暂无通用记忆（通用记忆对这台设备的所有客户会话都注入）</td></tr>'

            cleanup_logs = await memory_service.list_cleanup_logs(st["id"], limit=8)
            detail_html = f"""
    <div class="card">
        <h3>🧠 #{st['id']} {esc(st['name'])}</h3>
        <p class="card-sub">{esc(st['description']) or '（无描述）'}</p>
        {_set_form(f"/admin/memory/{st['id']}/update", org_id, st, agents_all)}
    </div>
    {_cleanup_card(st, org_id, view, cleanup_logs)}
    <div class="card">
        <h3>🌐 通用记忆</h3>
        <p class="card-sub">不绑定客户、对该智能体的所有客户会话都注入（如公司统一话术、通用政策）。</p>
        <div class="table-wrap"><table><tbody>{general_rows}</tbody></table></div>
        {general_more}
        <form method="POST" action="/admin/memory/{view}/item/create" class="form-row" style="margin-top:8px">
            <input type="hidden" name="org_id" value="{org_id}">
            <input type="hidden" name="view" value="{view}">
            <input type="hidden" name="customer_id" value="0">
            <div class="form-group" style="flex:1"><input type="text" name="content" required placeholder="补一条通用事实"></div>
            <button class="btn btn-outline btn-sm">添加</button>
        </form>
    </div>
    <div class="card">
        <h3>👥 客户与事实</h3>
        <form method="GET" action="/admin/memory" class="form-row" style="align-items:flex-end;margin-bottom:6px">
            <input type="hidden" name="view" value="{view}">
            <input type="hidden" name="org_id" value="{org_id}">
            {'<input type="hidden" name="all" value="1">' if all else ''}
            <div class="form-group" style="flex:1"><label>按客户筛选</label>
                <input type="text" name="q" value="{esc(q)}" placeholder="客户名或别名，留空看全部"></div>
            <button class="btn btn-outline btn-sm">筛选</button>
            {f'<a class="btn btn-ghost btn-sm" href="{_base(org_id, view)}">清空</a>' if q else ''}
        </form>
        <div class="form-row" style="align-items:flex-start">
            <form method="POST" action="/admin/memory/{view}/customer/create" class="form-row" style="flex:1">
                <input type="hidden" name="org_id" value="{org_id}">
                <input type="hidden" name="view" value="{view}">
                <div class="form-group" style="flex:1"><label>新增客户（规范名）</label><input type="text" name="name" required placeholder="如：张三"></div>
                <div class="form-group" style="flex:2"><label>别名（可选，逗号分隔）</label><input type="text" name="aliases" placeholder="如：张三-采购,张三(XX公司)"></div>
                <button class="btn btn-primary btn-sm">添加客户</button>
            </form>
        </div>
        <details style="margin:10px 0">
            <summary class="btn btn-outline btn-sm" style="cursor:pointer">📥 批量导入事实（一行一条）</summary>
            <p class="card-sub" style="margin-top:8px">组织创立之初按以往经验预置一套记忆：先填客户名（留空则导入到「通用」），一行一条事实。
            <b>客户只看规范名</b>（不看别名，不会因为名字撞上别人的别名就并进去）；<b>填了客户名就会建这位客户</b>；
            <b>事实留空 = 只建客户</b>。事实**只追加、绝不改写**：同客户下内容相同的行判重跳过（计入"重复"），
            已存在的记忆不会被这一行覆盖；超过 500 字的行跳过。</p>
            <form method="POST" action="/admin/memory/{view}/import" class="form-stack">
                <input type="hidden" name="org_id" value="{org_id}">
                <input type="hidden" name="view" value="{view}">
                <div class="form-group"><label>客户名（留空 = 通用记忆；<b>只看规范名，不看别名</b>）</label><input type="text" name="customer_name" placeholder="如：张三"></div>
                <div class="form-group"><label>别名（可选，<b>填了就以导入为准覆盖</b>；逗号分隔）</label><input type="text" name="aliases" placeholder="留空 = 不动该客户已有的别名，如：张三-采购,张三(XX公司)"></div>
                <div class="form-group"><label>事实（一行一条）</label><textarea name="text" rows="6" style="width:100%;font-family:var(--mono)"
                    placeholder="2026-08 询问过 A 型号报价，预算 5 万以内&#10;只接受账期 60 天&#10;技术对接人是李工"></textarea></div>
                <button class="btn btn-primary btn-sm">导入</button>
            </form>
        </details>
        {blocks}
    </div>"""

    content = f"""
    <div class="card">
        <h3>🧠 新建记忆集</h3>
        <p class="card-sub">长期记忆记录<b>客户事实</b>（谁、要什么、承诺过什么），与知识库（组织资料）分开存、分开检索。
        设备收到消息时按当前会话线索精确命中该客户的记忆并注入；智能体自己通过 <code>memory_save</code> 往该设备的「维护集」里记，
        这里的人工编辑是补充与前期准备。记忆<b>跟着设备走</b>（换智能体不换记忆，解绑设备即自动失效），绑定在「设备」页设置。</p>
        {_set_form(f"/admin/memory/create?org_id={org_id}" if org_id else "/admin/memory/create", org_id, agents=agents_all)}
    </div>
    {_limits_card()}
    {list_html}
    {detail_html}"""

    return page(
        "长期记忆",
        "🧠 长期记忆",
        content,
        msg,
        subtitle="客户事实的记忆集（智能体自己写 + 人工补充）",
        active="memory",
        org_id=org_id,
        org_name=cur_org_name,
        org_active="memory",
    )


# ── 记忆集操作 ──────────────────────────────────────


def _cleanup_card(st: dict, org_id: int, view: int, logs: List[dict] = None) -> str:
    """记忆清理：开关状态、上次清理、立即清理、最近记录。"""
    logs = logs or []
    agent_id = int(st.get("cleanup_agent_id") or 0)
    hours = int(st.get("cleanup_hours") or 0)
    label = cleanup_freq_label(hours)
    last = (st.get("last_cleanup_at") or "").replace("T", " ")[:16] or "从未清理"
    state = (
        f'<span class="badge badge-online">已开启</span> {label}'
        if hours > 0 and agent_id
        else '<span class="badge badge-offline">不清理</span>'
    )
    rows = "".join(
        f"<tr><td>{esc(l['customer_name'] or ('通用' if not l['customer_id'] else '#' + str(l['customer_id'])))}</td>"
        f"<td>{esc(l['status'])}</td><td>{l['before_count']} → {l['after_count']}</td>"
        f"<td>{esc(l['note'])}</td><td>{esc((l['created_at'] or '').replace('T', ' ')[:16])}</td></tr>"
        for l in logs
    ) or '<tr><td colspan="5" class="empty">还没有清理记录</td></tr>'
    return f"""
    <div class="card">
        <h3>🧹 记忆清理</h3>
        <p class="card-sub">开启后按频率自动整理：只处理<b>自上次清理以来有新增智能体事实</b>的客户，
        把重复/过时的旧事实压缩成更少的"当前状态"。<b>人工补录与批量导入的事实不参与清理</b>（会作为只读参考
        给智能体看，避免它重复记一遍）。清理会删除旧的智能体条目，所以每次都有记录可查。</p>
        <div class="inline-flex" style="gap:10px;align-items:center;flex-wrap:wrap">
            {state}
            <span class="muted">上次清理：{esc(last)}</span>
            <form method="POST" action="/admin/memory/{st['id']}/cleanup" style="display:inline">
                <input type="hidden" name="org_id" value="{org_id}">
                <input type="hidden" name="view" value="{view}">
                <button class="btn btn-outline btn-sm"{' disabled' if not agent_id else ''}>立即清理一次</button>
            </form>
        </div>
        <div class="table-wrap" style="margin-top:10px"><table>
            <thead><tr><th>客户</th><th>状态</th><th>条目变化</th><th>说明</th><th>时间</th></tr></thead>
            <tbody>{rows}</tbody>
        </table></div>
    </div>"""


@router.post("/admin/memory/{set_id}/cleanup")
async def admin_memory_cleanup(set_id: int, org_id: int = Query(0), view: int = Query(0)):
    """立即清理一次（不等频率到期）。"""
    try:
        r = await memory_cleanup_service.cleanup_set(set_id, trigger="manual")
        if not r.get("ok"):
            return _back(_base(org_id, view or set_id), f"❌ {esc(r.get('note') or '清理未执行')}")
        msg = (f"✅ 清理完成：{r['customers']} 位客户，条目 {r['before']} → {r['after']}"
               f"（跳过 {r['skipped']}，失败 {r['failed']}）")
        return _back(_base(org_id, view or set_id), msg)
    except Exception as e:
        logger.exception(f"记忆清理失败 set={set_id}")
        return _back(_base(org_id, view or set_id), f"❌ 清理失败: {esc(str(e))[:120]}")


@router.post("/admin/memory/create")
async def admin_memory_create(
    org_id: int = Query(0),
    name: str = Form(...),
    description: str = Form(""),
    cleanup_hours: int = Form(0),
    cleanup_agent_id: int = Form(0),
):
    if int(cleanup_hours or 0) > 0 and not int(cleanup_agent_id or 0):
        return _back(_base(org_id), "❌ 选了清理频率就必须指定用哪个智能体清理")
    try:
        new_id = await memory_service.create_set(
            name, description,
            cleanup_hours=cleanup_hours,
            cleanup_agent_id=cleanup_agent_id,
        )
        if org_id:
            await group_service.set_member_org_groups("memory", new_id, [org_id])
        return _back(_base(org_id), f"✅ 记忆集 #{new_id} 已创建")
    except Exception as e:
        logger.error(f"create memory set failed: {e}")
        return _back(_base(org_id), f"❌ {esc(str(e))}")


@router.post("/admin/memory/{set_id}/update")
async def admin_memory_update(
    set_id: int,
    org_id: int = Query(0),
    view: int = Query(0),
    name: str = Form(...),
    description: str = Form(""),
    cleanup_hours: int = Form(0),
    cleanup_agent_id: int = Form(0),
):
    if int(cleanup_hours or 0) > 0 and not int(cleanup_agent_id or 0):
        return _back(_base(org_id, view or set_id), "❌ 选了清理频率就必须指定用哪个智能体清理")
    try:
        await memory_service.update_set(set_id, name, description)
        await memory_service.set_cleanup_config(set_id, cleanup_hours, cleanup_agent_id)
        return _back(_base(org_id, view or set_id), "✅ 已保存")
    except Exception as e:
        return _back(_base(org_id, view or set_id), f"❌ {esc(str(e))}")


@router.get("/admin/memory/set/{set_id}/delete")
async def admin_memory_delete_set(set_id: int, org_id: int = Query(0)):
    await memory_service.delete_set(set_id)
    return _back(_base(org_id), "✅ 记忆集已删除")


# ── 客户操作 ────────────────────────────────────────


@router.post("/admin/memory/{set_id}/customer/create")
async def admin_memory_customer_create(
    set_id: int,
    org_id: int = Query(0),
    view: int = Query(0),
    name: str = Form(...),
    aliases: str = Form(""),
):
    try:
        # 与导入同一套规矩：**只看规范名**（名字撞上别人的别名就另建客户，不并进别人家）
        hit = await memory_service.resolve_customer(set_id, name, name_only=True)
        cid = int(hit[0]) if hit else await memory_service.add_customer(set_id, name, "")
        # 别名：填了就以这个表单为准**覆盖**；留空 = 不声明别名，不动已有的
        if cid and (aliases or "").strip():
            await memory_service.set_aliases(set_id, cid, aliases)
        return _back(_base(org_id, view or set_id), f"✅ 客户「{esc(name.strip())}」已添加")
    except Exception as e:
        return _back(_base(org_id, view or set_id), f"❌ {esc(str(e))}")


@router.post("/admin/memory/{set_id}/customer/{customer_id}/update")
async def admin_memory_customer_update(
    set_id: int,
    customer_id: int,
    org_id: int = Query(0),
    view: int = Query(0),
    name: str = Form(...),
    aliases: str = Form(""),
):
    try:
        await memory_service.update_customer(set_id, customer_id, name, aliases)
        return _back(_base(org_id, view or set_id), "✅ 客户已更新")
    except Exception as e:
        return _back(_base(org_id, view or set_id), f"❌ {esc(str(e))}")


@router.post("/admin/memory/{set_id}/customer/{customer_id}/merge")
async def admin_memory_customer_merge(
    set_id: int,
    customer_id: int,
    org_id: int = Query(0),
    view: int = Query(0),
    dst_id: int = Form(0),
):
    try:
        await memory_service.merge_customers(set_id, customer_id, dst_id)
        return _back(_base(org_id, view or set_id), "✅ 客户已合并")
    except Exception as e:
        return _back(_base(org_id, view or set_id), f"❌ {esc(str(e))}")


@router.get("/admin/memory/customer/{customer_id}/delete")
async def admin_memory_customer_delete(
    customer_id: int, set_id: int = Query(0), org_id: int = Query(0), view: int = Query(0)
):
    await memory_service.delete_customer(set_id, customer_id)
    return _back(_base(org_id, view or set_id), "✅ 客户及其事实已删除")


# ── 事实操作 ────────────────────────────────────────


@router.post("/admin/memory/{set_id}/item/create")
async def admin_memory_item_create(
    set_id: int,
    org_id: int = Query(0),
    view: int = Query(0),
    customer_id: int = Form(0),
    content: str = Form(...),
):
    try:
        added = await memory_service.add_item(set_id, customer_id, content, source="manual")
        return _back(
            _base(org_id, view or set_id), "✅ 已添加" if added else "⚠️ 已有相同内容，未重复添加"
        )
    except Exception as e:
        return _back(_base(org_id, view or set_id), f"❌ {esc(str(e))}")


@router.post("/admin/memory/{set_id}/item/{item_id}/update")
async def admin_memory_item_update(
    set_id: int,
    item_id: int,
    org_id: int = Query(0),
    view: int = Query(0),
    content: str = Form(...),
):
    try:
        await memory_service.update_item(set_id, item_id, content)
        return _back(_base(org_id, view or set_id), "✅ 已保存")
    except Exception as e:
        return _back(_base(org_id, view or set_id), f"❌ {esc(str(e))}")


@router.get("/admin/memory/item/{item_id}/delete")
async def admin_memory_item_delete(
    item_id: int, set_id: int = Query(0), org_id: int = Query(0), view: int = Query(0)
):
    await memory_service.delete_item(set_id, item_id)
    return _back(_base(org_id, view or set_id), "✅ 已删除")


@router.post("/admin/memory/{set_id}/import")
async def admin_memory_import(
    set_id: int,
    org_id: int = Query(0),
    view: int = Query(0),
    customer_name: str = Form(""),
    aliases: str = Form(""),
    text: str = Form(""),
):
    try:
        added, dup, skipped = await memory_service.import_items(
            set_id, text, customer_name=customer_name, aliases=aliases
        )
        return _back(
            _base(org_id, view or set_id),
            f"✅ 导入完成：新增 {added} 条，重复 {dup} 条，跳过 {skipped} 条",
        )
    except Exception as e:
        return _back(_base(org_id, view or set_id), f"❌ {esc(str(e))}")
