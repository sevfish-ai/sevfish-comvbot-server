"""
表格（自定义数据表）管理页面

- 创建表格（定义字段）→ 生成真实 MySQL 表
- 数据浏览 / 新增 / 编辑 / 删除
- 字段增删
- 授权给智能体（只读 / 读写），供 LLM 工具与工作流节点使用
"""
import json
import logging
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse

from ..models.table import FIELD_TYPES, FieldDef, TableCreateRequest
from ..services import table_service, agent_service, group_service
from ..layout import page, esc, _fmt_dt

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tables"])

_TYPE_LABELS = {
    "varchar": "文本",
    "int": "整数",
    "float": "小数",
    "datetime": "时间",
    "text": "长文本",
    "json": "JSON",
}


def _type_select(name: str, selected: str = "varchar", size: int = 90) -> str:
    # 属性统一用单引号：该 HTML 会被注入到 JS 字符串（addFieldRow）中，
    # 若用双引号会把外层 JS 字符串截断，导致页面 Script 语法错误、按钮失灵
    opts = "".join(
        f"<option value='{t}'{' selected' if t == selected else ''}>{_TYPE_LABELS[t]}</option>"
        for t in FIELD_TYPES
    )
    return f"<select name='{name}' class='sel-sm'>{opts}</select>"


def _field_rows_html(fields: list = None) -> str:
    """渲染字段定义表格的 <tbody> 行（供创建表格 / 模板预填复用）。

    fields 为模板预填字段列表（可选）；checkbox 的 value 为行号，按行对齐必填归属。
    属性统一单引号：该 HTML 会注入 addFieldRow 的 JS 字符串，双引号会截断 JS。
    """
    del_btn = (
        "<td><button type='button' class='btn btn-ghost btn-sm' "
        "onclick='this.closest(\\'tr\\').remove()'>删除</button></td>"
    )
    if not fields:
        return (
            "<tr>"
            "<td><input type='text' name='field_name[]' placeholder='如 order_no' required></td>"
            f"<td>{_type_select('field_type[]')}</td>"
            "<td><input type='text' name='field_label[]' placeholder='如 订单号'></td>"
            "<td style='text-align:center'><input type='checkbox' name='field_required[]' "
            "value='0' class='check'></td>"
            f"{del_btn}"
            "</tr>"
        )
    rows = []
    for i, f in enumerate(fields):
        fname = esc(str(f.get("name") or ""))
        ftype = str(f.get("type") or "varchar")
        flabel = esc(str(f.get("label") or ""))
        checked = " checked" if f.get("required") else ""
        rows.append(
            "<tr>"
            f"<td><input type='text' name='field_name[]' value='{fname}' placeholder='如 order_no' required></td>"
            f"<td>{_type_select('field_type[]', ftype)}</td>"
            f"<td><input type='text' name='field_label[]' value='{flabel}' placeholder='如 订单号'></td>"
            f"<td style='text-align:center'><input type='checkbox' name='field_required[]' "
            f"value='{i}'{checked} class='check'></td>"
            f"{del_btn}"
            "</tr>"
        )
    return "".join(rows)


@router.get("/admin/tables")
async def admin_tables(msg: str = "", template: int = 0, org_id: int = 0):
    """表格列表页（支持 ?template= 预填字段快速创建、?org_id= 按组织过滤）"""
    all_tables = await table_service.list_tables()
    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if not grp:
            return RedirectResponse(
                url="/admin/orgs?msg=❌ 组织不存在", status_code=303
            )
        cur_org_name = grp["name"]
        member_ids = set(grp["members"].get("table", []))
        all_tables = [t for t in all_tables if str(t.id) in member_ids]

    tables = all_tables
    table_groups = await group_service.list_org_groups()
    group_name = {g["id"]: g["name"] for g in table_groups}
    rows_parts = []
    for t in tables:
        sel_groups = set(await group_service.get_member_org_groups("table", t.id))
        groups_text = "、".join(
            esc(group_name[gid]) for gid in sorted(sel_groups) if gid in group_name
        )
        rows_parts.append(
            f"<tr>"
            f"<td><code>#{t.id}</code></td>"
            f"<td><span class='cell-main'>{esc(t.name)}</span>"
            f"<span class='cell-sub'>{esc(t.description)}</span></td>"
            f"<td>{t.field_count}</td>"
            f"<td>{t.row_count}</td>"
            f"<td>{t.grant_count} 个</td>"
            f"<td>{groups_text or '—'}</td>"
            f"<td>{_fmt_dt(t.created_at)}</td>"
            f"<td>"
            f'<a href="/admin/table/{t.id}{f"?org_id={org_id}" if org_id else ""}" class="btn btn-primary btn-sm">管理数据</a> '
            f'<a href="/admin/table/{t.id}/delete{f"?org_id={org_id}" if org_id else ""}" class="btn btn-danger btn-sm" '
            f"onclick=\"return confirm('删除表格「{esc(t.name)}」？物理表数据将一并删除！')\">删除</a>"
            f"</td>"
            f"</tr>"
        )
    rows_html = (
        "\n".join(rows_parts) if rows_parts else '<tr><td colspan="8" class="empty">暂无表格，先在上方创建</td></tr>'
    )

    # 模板预填
    template_obj = None
    if template:
        template_obj = await table_service.get_template(template)
    field_rows_html = _field_rows_html(template_obj["fields"] if template_obj else None)
    tpl_notice = (
        f'<div class="note note-blue mb-0" style="padding:9px 14px;margin-bottom:10px">'
        f'📋 已从模板「{esc(template_obj["name"])}」预填字段，可修改后创建'
        f'</div>'
        if template_obj
        else ""
    )

    type_select_html = _type_select("field_type[]")
    create_form = f"""
    <form method="POST" action="/admin/table/create" class="form-stack">
        <input type="hidden" name="org_id" value="{org_id}">
        {tpl_notice}
        <div class="form-grid">
            <div class="form-group"><label>表格名称 *（中文展示名）</label><input type="text" name="name" placeholder="如：物流订单" required></div>
            <div class="form-group"><label>描述</label><input type="text" name="description" placeholder="这张表用来存什么"></div>
        </div>
        <div>
            <label class="field-label">字段定义（至少 1 个）</label>
            <div class="table-wrap">
            <table id="fieldTbl">
                <thead><tr><th style="width:160px">字段名 *</th><th>类型</th><th style="width:220px">中文名</th><th style="width:80px">必填</th><th></th></tr></thead>
                <tbody>
                    {field_rows_html}
                </tbody>
            </table>
            </div>
            <button type="button" class="btn btn-ghost btn-sm" onclick="addFieldRow()" style="margin-top:8px">+ 添加字段</button>
        </div>
        <div><button type="submit" class="btn btn-primary">创建表格</button></div>
    </form>
    <script>
    function addFieldRow(){{
        var tb=document.getElementById('fieldTbl').querySelector('tbody');
        var tr=document.createElement('tr');
        tr.innerHTML="<td><input type='text' name='field_name[]' placeholder='如 order_no' required></td>"+
            "<td>{type_select_html}</td>"+
            "<td><input type='text' name='field_label[]' placeholder='如 订单号'></td>"+
            "<td style='text-align:center'><input type='checkbox' name='field_required[]' value='" + tb.rows.length + "' class='check'></td>"+
            "<td><button type='button' class='btn btn-ghost btn-sm' onclick='this.closest(\\'tr\\').remove()'>删除</button></td>";
        tb.appendChild(tr);
    }}
    </script>
    """

    content = f"""
    <div class="card">
        <div class="card-hd">
            <h3>📋 新建表格</h3>
            <a href="/admin/table/templates{f'?org_id={org_id}' if org_id else ''}" class="btn btn-ghost btn-sm">📋 模板管理</a>
        </div>
        <p class="card-sub">每张表格对应一张真实 MySQL 表。创建后可授权给智能体，智能体在工作流或对话中读写这张表。</p>
        {create_form}
    </div>
    <div class="card">
        <h3>🗄️ 表格列表</h3>
        <div class="table-wrap">
        <table>
            <thead><tr><th>ID</th><th>名称</th><th>字段数</th><th>数据行</th><th>授权智能体</th><th>组织</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
        </div>
    </div>
    """
    return page(
        "表格管理", "🗄️ 表格管理", content, msg, active="tables",
        org_id=org_id, org_name=cur_org_name, org_active="tables",
    )


# ════════════════════════════════════════════
#  表格模板（快速创建表格）
# ════════════════════════════════════════════

@router.get("/admin/table/templates")
async def admin_table_templates(msg: str = "", org_id: int = 0):
    """表格模板管理页：新建模板 + 模板列表

    模板本身是全局资源，不属于任何组织；但 org_id 上下文需透传，
    保证在组织内「用它创建表格」时，新表格仍归属当前组织。
    """
    templates = await table_service.list_templates()
    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if grp:
            cur_org_name = grp["name"]
    oid_q = f"?org_id={org_id}" if org_id else ""
    type_select_html = _type_select("field_type[]")
    create_form = f"""
    <form method="POST" action="/admin/table/template/create" class="form-stack">
        <div class="form-grid">
            <div class="form-group"><label>模板名称 *</label><input type="text" name="name" placeholder="如：物流订单表" required></div>
            <div class="form-group"><label>描述</label><input type="text" name="description" placeholder="这份模板用来存什么"></div>
        </div>
        <div>
            <label class="field-label">字段定义（至少 1 个，作为模板的默认结构）</label>
            <div class="table-wrap">
            <table id="fieldTbl">
                <thead><tr><th style="width:160px">字段名 *</th><th>类型</th><th style="width:220px">中文名</th><th style="width:80px">必填</th><th></th></tr></thead>
                <tbody>{_field_rows_html()}</tbody>
            </table>
            </div>
            <button type="button" class="btn btn-ghost btn-sm" onclick="addFieldRow()" style="margin-top:8px">+ 添加字段</button>
        </div>
        <div><button type="submit" class="btn btn-primary">保存模板</button></div>
    </form>
    <script>
    function addFieldRow(){{
        var tb=document.getElementById('fieldTbl').querySelector('tbody');
        var tr=document.createElement('tr');
        tr.innerHTML="<td><input type='text' name='field_name[]' placeholder='如 order_no' required></td>"+
            "<td>{type_select_html}</td>"+
            "<td><input type='text' name='field_label[]' placeholder='如 订单号'></td>"+
            "<td style='text-align:center'><input type='checkbox' name='field_required[]' value='" + tb.rows.length + "' class='check'></td>"+
            "<td><button type='button' class='btn btn-ghost btn-sm' onclick='this.closest(\\'tr\\').remove()'>删除</button></td>";
        tb.appendChild(tr);
    }}
    </script>
    """

    tpl_rows = ""
    for tpl in templates:
        field_summary = "、".join(
            f"{esc(f.get('name'))} ({_TYPE_LABELS.get(str(f.get('type') or ''), str(f.get('type') or ''))})"
            for f in tpl["fields"][:6]
        ) + ("…" if len(tpl["fields"]) > 6 else "")
        tpl_rows += (
            f"<tr>"
            f"<td><code>#{tpl['id']}</code></td>"
            f"<td><span class='cell-main'>{esc(tpl['name'])}</span>"
            f"<span class='cell-sub'>{esc(tpl['description'])}</span></td>"
            f"<td>{tpl['field_count']} 个</td>"
            f"<td class='muted' style='font-size:12px'>{field_summary or '—'}</td>"
            f"<td>{_fmt_dt(tpl['created_at'])}</td>"
            f"<td>"
            f'<a href="/admin/tables?template={tpl["id"]}{f"&org_id={org_id}" if org_id else ""}" class="btn btn-primary btn-sm">用它创建表格</a> '
            f'<a href="/admin/table/template/{tpl["id"]}/delete{f"?org_id={org_id}" if org_id else ""}" class="btn btn-danger btn-sm" '
            f"onclick=\"return confirm('删除模板「{esc(tpl['name'])}」？不影响已创建的表格。')\">删除</a>"
            f"</td>"
            f"</tr>"
        )
    tpl_rows_html = tpl_rows or '<tr><td colspan="6" class="empty">暂无模板</td></tr>'

    content = f"""
    <div class="card">
        <div class="card-hd">
            <h3>📋 新建模板</h3>
            <a href="/admin/tables{oid_q}" class="btn btn-ghost btn-sm">← 返回表格管理</a>
        </div>
        <p class="card-sub">把常用表结构存为模板，之后在「表格管理」一键创建。模板属于全局资源，不归属于任何组织。</p>
        {create_form}
    </div>
    <div class="card">
        <h3>🗄️ 模板列表</h3>
        <div class="table-wrap">
        <table>
            <thead><tr><th>ID</th><th>名称</th><th>字段数</th><th>字段</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody>{tpl_rows_html}</tbody>
        </table>
        </div>
    </div>
    """
    return page(
        "表格模板", "📋 表格模板", content, msg, active="/admin/tables",
        org_id=org_id, org_name=cur_org_name, org_active="tables",
    )


@router.post("/admin/table/template/create")
async def admin_table_template_create(request: Request):
    form = await request.form()
    name = (form.get("name") or "").strip()
    description = form.get("description") or ""
    field_name = form.getlist("field_name[]")
    field_type = form.getlist("field_type[]")
    field_label = form.getlist("field_label[]")
    required_set = set()
    for v in form.getlist("field_required[]"):
        try:
            required_set.add(int(v))
        except (TypeError, ValueError):
            pass
    try:
        fields = []
        for i, fname in enumerate(field_name):
            fname = (fname or "").strip()
            if not fname:
                continue
            fields.append(
                {
                    "name": fname,
                    "type": field_type[i] if i < len(field_type) else "varchar",
                    "label": field_label[i] if i < len(field_label) else "",
                    "required": i in required_set,
                }
            )
        tpl_id = await table_service.create_template(name, description, fields)
        return RedirectResponse(
            url=f"/admin/table/templates?msg={quote('✅ 模板已保存 (#' + str(tpl_id) + ')')}", status_code=303
        )
    except Exception as e:
        logger.error(f"create template failed: {e}")
        return RedirectResponse(
            url=f"/admin/table/templates?msg=❌ {esc(str(e))}", status_code=303
        )


@router.get("/admin/table/template/{template_id}/delete")
async def admin_table_template_delete(template_id: int, org_id: int = Query(0)):
    try:
        ok = await table_service.delete_template(template_id)
        return RedirectResponse(
            url=f"/admin/table/templates{f'?org_id={org_id}' if org_id else ''}{'&' if org_id else '?'}msg={'✅ 模板已删除' if ok else '❌ 模板不存在'}",
            status_code=303,
        )
    except Exception as e:
        logger.error(f"delete template failed: {e}")
        return RedirectResponse(
            url=f"/admin/table/templates{f'?org_id={org_id}' if org_id else ''}{'&' if org_id else '?'}msg=❌ {esc(str(e))}",
            status_code=303,
        )


@router.post("/admin/table/create")
async def admin_table_create(request: Request):
    # 数组字段在 HTML 中的 name 带 []（如 field_name[]），FastAPI 的
    # `List[str] = Form([])` 匹配不到带 [] 的键名，必须用 request.form().getlist()
    form = await request.form()
    name = (form.get("name") or "").strip()
    description = form.get("description") or ""
    org_id = 0
    try:
        org_id = int(form.get("org_id") or 0)
    except (TypeError, ValueError):
        org_id = 0
    field_name = form.getlist("field_name[]")
    field_type = form.getlist("field_type[]")
    field_label = form.getlist("field_label[]")
    # field_required[]: checkbox 的 value 为字段行号（勾选才提交），按行对齐
    required_set = set()
    for v in form.getlist("field_required[]"):
        try:
            required_set.add(int(v))
        except (TypeError, ValueError):
            pass
    try:
        fields = []
        for i, fname in enumerate(field_name):
            fname = (fname or "").strip()
            if not fname:
                continue
            ftype = field_type[i] if i < len(field_type) else "varchar"
            flabel = field_label[i] if i < len(field_label) else ""
            fields.append(
                {
                    "name": fname,
                    "type": ftype,
                    "label": flabel,
                    "required": i in required_set,
                }
            )
        table_id = await table_service.create_table(
            TableCreateRequest(name=name, description=description, fields=fields)
        )
        # 在组织上下文中创建则自动归属该组织（创建后不可改）
        if org_id:
            await group_service.set_member_org_groups("table", table_id, [org_id])
        base = f"/admin/table/{table_id}?org_id={org_id}" if org_id else f"/admin/table/{table_id}"
        return RedirectResponse(
            url=f"{base}{'&' if '?' in base else '?'}msg=✅ 表格已创建", status_code=303
        )
    except Exception as e:
        logger.error(f"create table failed: {e}")
        base = f"/admin/tables?org_id={org_id}" if org_id else "/admin/tables"
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ {esc(str(e))}", status_code=303)


@router.get("/admin/table/{table_id}")
async def admin_table_detail(table_id: int, msg: str = "", org_id: int = 0):
    """表格详情：字段 + 数据 + 授权"""
    table = await table_service.get_table(table_id)
    if not table:
        return RedirectResponse(url="/admin/tables?msg=❌ 表格不存在", status_code=303)
    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if grp:
            cur_org_name = grp["name"]
    table_key = table["table_key"]
    fields = table["fields"]

    # ── 数据行 ──
    try:
        rows = await table_service.query_rows(table_key, limit=200)
    except Exception as e:
        logger.error(f"query rows failed: {e}")
        rows = []
    grants = await table_service.get_agent_grants(table_id)
    agents = await agent_service.list_agents()

    # ── 数据表格 ──
    head = "".join(
        f"<th>{esc(f['label'] or f['name'])}<span class='cell-sub mono'>{esc(f['name'])}</span></th>"
        for f in fields
    )
    insert_inputs = "".join(
        f"<td><input type='text' name='{esc(f['name'])}' placeholder='{esc(f['name'])}' "
        f"{'required' if f['required'] else ''}></td>"
        for f in fields
    )
    row_parts = []
    for r in rows:
        cells = ""
        for f in fields:
            val = r.get(f["name"])
            if val is None:
                cells += "<td class='muted'>—</td>"
            elif f["type"] == "json":
                cells += (
                    "<td style='max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>"
                    f"{esc(json.dumps(val, ensure_ascii=False))}</td>"
                )
            else:
                cells += f"<td>{esc(str(val))}</td>"
        edit_fields = "".join(
            f"<div class='inline-flex' style='gap:6px;margin-right:10px'>"
            f"<label class='muted' style='font-size:11px'>{esc(f['name'])}</label>"
            f"<input type='text' name='{esc(f['name'])}' value='{esc(str(r.get(f['name']) or ''))}' class='inp-sm'></div>"
            for f in fields
        )
        row_parts.append(
            f"<tr>"
            f"<td><code>#{r['id']}</code></td>{cells}"
            f"<td>"
            f"<button class='btn btn-ghost btn-sm' onclick=\"toggleEdit({r['id']})\">编辑</button> "
            f'<a href="/admin/table/{table_id}/row/{r["id"]}/delete{f"?org_id={org_id}" if org_id else ""}" class="btn btn-danger btn-sm" '
            f"onclick=\"return confirm('删除此行 #id？')\">删除</a>"
            f"</td></tr>"
            f"<tr id='edit-{r['id']}' style='display:none;background:#f8fafc'>"
            f"<td colspan='{len(fields) + 2}'>"
            f"<form method='POST' action='/admin/table/{table_id}/row/{r['id']}/update{f'?org_id={org_id}' if org_id else ''}' class='inline-flex' style='gap:10px'>"
            f"{edit_fields}"
            f"<button class='btn btn-primary btn-sm'>保存</button>"
            f"</form></td></tr>"
        )
    if not row_parts:
        row_parts.append(
            f'<tr><td colspan="{len(fields) + 2}" class="empty">暂无数据</td></tr>'
        )
    data_html = f"""
    <form method="POST" action="/admin/table/{table_id}/row/insert{f'?org_id={org_id}' if org_id else ''}" class="inline-flex" style="margin-bottom:14px;gap:8px">
        {insert_inputs}
        <button class="btn btn-success btn-sm">新增一行</button>
    </form>
    <div class="table-wrap">
    <table>
        <thead><tr><th>ID</th>{head}<th>操作</th></tr></thead>
        <tbody>{"".join(row_parts)}</tbody>
    </table>
    </div>"""

    # ── 字段管理 ──
    field_rows = ""
    for f in fields:
        field_rows += (
            f"<tr><td><code>{esc(f['name'])}</code></td>"
            f"<td>{_TYPE_LABELS.get(f['type'], f['type'])}</td>"
            f"<td>{esc(f['label'])}</td>"
            f"<td>{'是' if f['required'] else '—'}</td>"
            f'<td><a href="/admin/table/{table_id}/field/{f["id"]}/delete{f"?org_id={org_id}" if org_id else ""}" class="btn btn-danger btn-sm" '
            f"onclick=\"return confirm('删除字段 {esc(f['name'])}？该列数据将丢失！')\">删除</a></td></tr>"
        )
    fields_html = f"""
    <div class="table-wrap">
    <table>
        <thead><tr><th>字段名</th><th>类型</th><th>中文名</th><th>必填</th><th>操作</th></tr></thead>
        <tbody>{field_rows}</tbody>
    </table>
    </div>
    <form method="POST" action="/admin/table/{table_id}/field/add{f'?org_id={org_id}' if org_id else ''}" class="inline-flex" style="margin-top:12px;gap:10px">
        <div class="form-group"><label>字段名</label><input type="text" name="field_name" placeholder="如 new_field" required></div>
        <div class="form-group"><label>类型</label>{_type_select("field_type")}</div>
        <div class="form-group"><label>中文名</label><input type="text" name="field_label" placeholder="字段中文名"></div>
        <button class="btn btn-primary btn-sm">添加字段</button>
    </form>"""

    # ── 授权 ──
    grant_rows = ""
    for a in agents:
        perm = grants.get(a.id)
        grant_rows += (
            f"<tr>"
            f"<td><code>#{a.id}</code> {esc(a.name)}</td>"
            f"<td>"
            f'<select name="permissions[]" class="sel-sm">'
            f'<option value="0"{(" selected" if perm == 0 else "")}>只读</option>'
            f'<option value="1"{(" selected" if perm == 1 else "")}>读写</option>'
            f"</select>"
            f"</td>"
            f"<td><input type='checkbox' name='agent_ids[]' value='{a.id}'{(' checked' if perm is not None else '')} "
            f"class='check' onchange='toggleGrant(this)'></td>"
            f"</tr>"
        )
    grants_html = f"""
    <form method="POST" action="/admin/table/{table_id}/grant{f'?org_id={org_id}' if org_id else ''}" class="form-row" style="align-items:flex-start">
        <div style="flex:1">
            <div class="table-wrap">
            <table>
                <thead><tr><th>智能体</th><th>权限</th><th>授权</th></tr></thead>
                <tbody>{grant_rows or '<tr><td colspan="3" class="empty">暂无智能体，请先到「智能体」页创建</td></tr>'}</tbody>
            </table>
            </div>
        </div>
        <button class="btn btn-primary">保存授权</button>
    </form>"""

    content = f"""
    <div class="card">
        <div class="card-hd">
            <h3>🗄️ {esc(table['name'])} <span class="muted" style="font-size:13px">(table_key: {esc(table_key)})</span></h3>
            <a href="/admin/tables{f'?org_id={org_id}' if org_id else ''}" class="btn btn-ghost btn-sm">← 返回列表</a>
        </div>
        <p class="card-sub">{esc(table['description'])}</p>
    </div>
    <div class="card">
        <h3>📊 数据（{len(rows)} 行）</h3>
        {data_html}
    </div>
    <div class="card">
        <h3>🧩 字段定义</h3>
        {fields_html}
    </div>
    <div class="card">
        <h3>🔑 授权给智能体</h3>
        <p class="card-sub">勾选并保存后，被授权的智能体可通过「联网工具」或工作流中的表格节点读写本表。</p>
        {grants_html}
    </div>
    <script>
    function toggleEdit(id){{ var el=document.getElementById('edit-'+id); if(el) el.style.display=(el.style.display==='none')?'':'none'; }}
    function toggleGrant(cb){{
        var tr=cb.closest('tr');
        var sel=tr.querySelector('select');
        if(sel) sel.disabled=!cb.checked;
    }}
    document.querySelectorAll('input[name="agent_ids[]"]').forEach(function(cb){{
        var sel=cb.closest('tr').querySelector('select');
        if(sel) sel.disabled=!cb.checked;
    }});
    </script>
    """
    return page(
        f"表格 {table['name']}", f"🗄️ {esc(table['name'])}", content, msg,
        active="/admin/tables",
        org_id=org_id, org_name=cur_org_name, org_active="tables",
    )


# ── POST 操作 ──────────────────────────────────


@router.post("/admin/table/{table_id}/field/add")
async def admin_table_field_add(
    table_id: int,
    field_name: str = Form(...),
    field_type: str = Form("varchar"),
    field_label: str = Form(""),
    org_id: int = Query(0),
):
    base = f"/admin/table/{table_id}?org_id={org_id}" if org_id else f"/admin/table/{table_id}"
    try:
        ok = await table_service.add_field(
            table_id, FieldDef(name=field_name, type=field_type, label=field_label)
        )
        if not ok:
            return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ 表格不存在", status_code=303)
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=✅ 字段已添加", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ {esc(str(e))}", status_code=303)


@router.get("/admin/table/{table_id}/field/{field_id}/delete")
async def admin_table_field_delete(table_id: int, field_id: int, org_id: int = Query(0)):
    base = f"/admin/table/{table_id}?org_id={org_id}" if org_id else f"/admin/table/{table_id}"
    try:
        ok = await table_service.remove_field(table_id, field_id)
        if not ok:
            return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ 字段不存在", status_code=303)
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=✅ 字段已删除", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ {esc(str(e))}", status_code=303)


@router.get("/admin/table/{table_id}/delete")
async def admin_table_delete(table_id: int, org_id: int = Query(0)):
    try:
        ok = await table_service.delete_table(table_id)
        if not ok:
            return RedirectResponse(url="/admin/tables?msg=❌ 表格不存在", status_code=303)
        base = f"/admin/tables?org_id={org_id}" if org_id else "/admin/tables"
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=✅ 表格已删除", status_code=303)
    except Exception as e:
        base = f"/admin/tables?org_id={org_id}" if org_id else "/admin/tables"
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ {esc(str(e))}", status_code=303)


@router.post("/admin/table/{table_id}/grant")
async def admin_table_grant(table_id: int, request: Request, org_id: int = Query(0)):
    # agent_ids[]/permissions[] 是带 [] 的数组字段，须用 getlist 读取
    form = await request.form()
    agent_ids = form.getlist("agent_ids[]")
    permissions = form.getlist("permissions[]")
    base = f"/admin/table/{table_id}?org_id={org_id}" if org_id else f"/admin/table/{table_id}"
    try:
        grants = {}
        for i, aid in enumerate(agent_ids):
            perm = permissions[i] if i < len(permissions) else "0"
            grants[int(aid)] = 1 if perm == "1" else 0
        await table_service.set_agent_grants(table_id, grants)
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=✅ 授权已保存", status_code=303)
    except Exception as e:
        logger.error(f"grant table failed: {e}")
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ {esc(str(e))}", status_code=303)


@router.post("/admin/table/{table_id}/row/insert")
async def admin_table_row_insert(table_id: int, request: Request, org_id: int = Query(0)):
    table = await table_service.get_table(table_id)
    base = f"/admin/table/{table_id}?org_id={org_id}" if org_id else f"/admin/table/{table_id}"
    if not table:
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ 表格不存在", status_code=303)
    form = await request.form()
    data = {}
    for f in table["fields"]:
        val = form.get(f["name"])
        data[f["name"]] = _parse_field_value(f, val)
    try:
        await table_service.insert_row(table["table_key"], data)
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=✅ 已新增", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ {esc(str(e))}", status_code=303)


@router.post("/admin/table/{table_id}/row/{row_id}/update")
async def admin_table_row_update(table_id: int, row_id: int, request: Request, org_id: int = Query(0)):
    table = await table_service.get_table(table_id)
    base = f"/admin/table/{table_id}?org_id={org_id}" if org_id else f"/admin/table/{table_id}"
    if not table:
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ 表格不存在", status_code=303)
    form = await request.form()
    data = {}
    for f in table["fields"]:
        if f["name"] in form:
            data[f["name"]] = _parse_field_value(f, form.get(f["name"]))
    try:
        ok = await table_service.update_row(table["table_key"], row_id, data)
        return RedirectResponse(
            url=f"{base}{'&' if '?' in base else '?'}msg={'✅ 已更新' if ok else '❌ 行不存在或未修改'}",
            status_code=303,
        )
    except Exception as e:
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ {esc(str(e))}", status_code=303)


@router.get("/admin/table/{table_id}/row/{row_id}/delete")
async def admin_table_row_delete(table_id: int, row_id: int, org_id: int = Query(0)):
    table = await table_service.get_table(table_id)
    base = f"/admin/table/{table_id}?org_id={org_id}" if org_id else f"/admin/table/{table_id}"
    if not table:
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ 表格不存在", status_code=303)
    try:
        ok = await table_service.delete_row(table["table_key"], row_id)
        return RedirectResponse(
            url=f"{base}{'&' if '?' in base else '?'}msg={'✅ 已删除' if ok else '❌ 行不存在'}",
            status_code=303,
        )
    except Exception as e:
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ {esc(str(e))}", status_code=303)


def _parse_field_value(field: dict, raw):
    """按字段类型转换表单提交的值（空串 → None）"""
    raw = (raw or "").strip()
    if raw == "":
        return None
    ftype = field.get("type")
    if ftype == "int":
        try:
            return int(raw)
        except ValueError:
            return raw
    if ftype == "float":
        try:
            return float(raw)
        except ValueError:
            return raw
    if ftype == "json":
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return raw
