"""
技能（Skill）管理页：场景化行为指导配置（软），不承载安全强制。

技能 = 触发条件（群类型/关键词/正则） + 注入给 LLM 的行为指导文本。
消息到达时按会话上下文匹配，命中则把技能指令拼进该次 LLM 调用的 system 消息。
"""

import json
import logging
from typing import List

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from ..services import skill_service, agent_service, group_service
from ..services.skill_service import bad_patterns
from ..layout import page, esc, _fmt_dt

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])


def _skill_form(action: str, org_id: int = 0, sk: dict = None) -> str:
    r"""新建/编辑共用一个表单。sk=None 为新建，传技能 dict 为编辑（回填 + 改提交地址）"""
    sk = sk or {}
    trg = sk.get("trigger") or {}
    is_edit = bool(sk.get("id"))
    rt = {int(x) for x in (trg.get("room_types") or [])}
    oid = f'<input type="hidden" name="org_id" value="{org_id}">' if org_id else ""
    rt_boxes = "".join(
        f'<label class="chip-check"><input type="checkbox" name="room_types" value="{v}"'
        f'{(" checked" if v in rt else "")}> {label}</label>'
        for v, label in ((1, "外部群"), (3, "内部群"), (2, "外部联系人"), (4, "内部联系人"))
    )
    return f"""
        <form method="POST" action="{action}" class="form-row" style="align-items:flex-start">
            {oid}
            <div class="form-group" style="flex:1"><label>技能名称 *</label><input type="text" name="name" placeholder="如：报修群自动登记" required value="{esc(sk.get('name', ''))}"></div>
            <div class="form-group" style="flex:2"><label>描述</label><input type="text" name="description" placeholder="这条技能做什么" value="{esc(sk.get('description', ''))}"></div>
            <div class="form-group full" style="flex-basis:100%">
                <label>触发群类型（选填，可多选）</label>
                <div class="inline-flex" style="gap:8px;flex-wrap:wrap">{rt_boxes}</div>
                <div class="form-hint">群类型与其余条件按 AND 组合；会话名/成员名（范围）与正文关键词/正则（内容）各自组内任一命中，<b>同时配置范围与内容时两者都命中才触发</b>（避免无关话题误触发）；只配范围=该会话/该成员所有消息触发；全部留空=永不触发</div>
            </div>
            <div class="form-group" style="flex:1"><label>关键词（选填，逗号分隔，任一命中即触发）</label><input type="text" name="keywords" placeholder="如：报修,维修" value="{esc(','.join(trg.get('keywords') or []))}"></div>
            <div class="form-group" style="flex:1"><label>正则匹配（选填，<b>一行一条</b>，任一命中即触发）</label><textarea name="patterns" rows="3" style="font-family:var(--mono)" placeholder="工单号[:：]\\s*\\d{10}&#10;^(报修|维修)[:：]&#10;\\d{1,3}(,\\d{3})*元">{esc(chr(10).join(trg.get('patterns') or []))}</textarea></div>
            <div class="form-group" style="flex:1"><label>会话名关键词（选填，逗号分隔，群名或私聊对方昵称包含即触发）</label><input type="text" name="room_keywords" placeholder="如：供应商,项目部,张三" value="{esc(','.join(trg.get('room_keywords') or []))}"></div>
            <div class="form-group" style="flex:1"><label>成员名关键词（选填，逗号分隔，发送人名称包含即触发，仅群聊有效）</label><input type="text" name="speaker_keywords" placeholder="如：张三,李总" value="{esc(','.join(trg.get('speaker_keywords') or []))}"></div>
            <div class="form-group full" style="flex-basis:100%">
                <label>技能指令（注入给 LLM 的行为指导）*</label>
                <textarea name="content" rows="3" style="font-family:var(--mono)"
                    placeholder="如：当收到报修类消息时：调用 run_workflow_5 登记工单；执行完成后静默返回，不要生成任何回复文本。" required>{esc(sk.get('content', ''))}</textarea>
            </div>
            <button class="btn btn-primary">{'保存修改' if is_edit else '创建技能'}</button>
        </form>"""


@router.get("/admin/skills", response_class=HTMLResponse)
async def admin_skills(msg: str = "", org_id: int = 0, skill_id: int = 0):
    """技能列表 + 新建/编辑表单（skill_id 指定则上方表单进入编辑态）"""
    skills = await skill_service.list_skills()
    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if grp:
            cur_org_name = grp["name"]

    editing = next((s for s in skills if s["id"] == skill_id), None) if skill_id else None
    if skill_id and not editing:
        msg = msg or "❌ 技能不存在（可能已被删除）"

    rows = []
    for s in skills:
        trg = s["trigger"] or {}
        trg_parts = []
        if trg.get("room_types"):
            trg_parts.append("群:" + ",".join(str(x) for x in trg["room_types"]))
        if trg.get("keywords"):
            trg_parts.append("词:" + ",".join(str(x) for x in trg["keywords"]))
        if trg.get("patterns"):
            pats = trg["patterns"]
            bad = bad_patterns(pats)
            tag = "正则" + ("⚠非法" if bad else "")
            trg_parts.append(f"{tag}:" + ",".join(str(x) for x in pats))
        if trg.get("room_keywords"):
            trg_parts.append("会话:" + ",".join(str(x) for x in trg["room_keywords"]))
        if trg.get("speaker_keywords"):
            trg_parts.append("成员:" + ",".join(str(x) for x in trg["speaker_keywords"]))
        rows.append(
            f"<tr>"
            f"<td><code>#{s['id']}</code></td>"
            f"<td><b>{esc(s['name'])}</b><br><span class='muted'>{esc(s['description'])}</span></td>"
            f"<td style='font-size:12px'>{esc('；'.join(trg_parts)) or '—'}</td>"
            f"<td style='max-width:280px;font-family:var(--mono);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>{esc(s['content'])}</td>"
            f"<td><span class='muted'>智能体勾选</span></td>"
            f"<td>{'<span class="badge badge-online">启用</span>' if s['enabled'] else '<span class="badge badge-offline">停用</span>'}</td>"
            f"<td>{_fmt_dt(s['created_at'])}</td>"
            f"<td>"
            f"<button type='button' class='btn btn-ghost btn-sm' onclick=\"toggleEdit({s['id']})\">编辑</button> "
            f'<form method="POST" action="/admin/skills/{s["id"]}/toggle" class="inline-form">'
            f'<button class="btn btn-ghost btn-sm">{("停用" if s["enabled"] else "启用")}</button></form> '
            f'<a href="/admin/skills/{s["id"]}/delete" class="btn btn-danger btn-sm" '
            f"onclick=\"return confirm('删除技能 {esc(s['name'])}？')\">删除</a>"
            f"</td></tr>"
            f"<tr id='edit-{s['id']}' style='display:none;background:#f8fafc'>"
            f"<td colspan='8'>{_skill_form(f'/admin/skills/{s['id']}/update', org_id, s)}</td>"
            f"</tr>"
        )
    rows_html = (
        "\n".join(rows)
        if rows
        else '<tr><td colspan="8" class="empty">暂无技能，先在上方创建</td></tr>'
    )

    content = f"""
    <div class="card">
        <h3>🧩 {'编辑技能 #' + str(editing['id']) if editing else '新建技能'}</h3>
        <p class="card-sub">技能 = 触发条件 + 行为指导指令。消息到达时按条件匹配，命中后把指令注入本次 LLM 调用。
        <b>启用方式：技能创建后在「智能体管理 → 编辑智能体」中勾选，勾选该技能的智能体才会触发（智能体侧白名单）。
        注意：技能是给 LLM 的行为建议（软），安全强制由工具授权/工作流校验承担。</b></p>
        {_skill_form(f"/admin/skills/{editing['id']}/update" if editing else "/admin/skills/create", org_id, editing)}
    </div>
    <div class="card">
        <h3>📋 技能列表</h3>
        <div class="table-wrap">
        <table>
            <thead><tr><th>ID</th><th>名称</th><th>触发条件</th><th>指令内容</th><th>启用方式</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
        </div>
    </div>
    <script>
    function toggleEdit(id){{ var el=document.getElementById('edit-'+id); if(!el) return;
      el.style.display=(el.style.display==='none')?'':'none';
      if(el.style.display==='') el.scrollIntoView({{block:'center',behavior:'smooth'}}); }}
    </script>
    """
    return page(
        "技能",
        "🧩 技能管理",
        content,
        msg,
        subtitle="场景化行为指导（软）",
        active="skills",
        org_id=org_id,
        org_name=cur_org_name,
        org_active="skills",
    )


def _build_trigger(room_types, keywords, patterns, room_keywords, speaker_keywords) -> dict:
    """表单字段 → trigger dict；正则无法编译直接抛错（别把废技能写进库）"""
    trg = {
        "room_types": [int(x) for x in room_types],
        "keywords": [k.strip() for k in keywords.split(",") if k.strip()],
        "patterns": skill_service._split_patterns(patterns),
        "room_keywords": [k.strip() for k in room_keywords.split(",") if k.strip()],
        "speaker_keywords": [k.strip() for k in speaker_keywords.split(",") if k.strip()],
    }
    bad = bad_patterns(trg["patterns"])
    if bad:
        raise ValueError(f"正则无法编译：{'、'.join(bad)}（请检查括号/转义）")
    return trg


def _skill_base(org_id: int) -> str:
    return f"/admin/skills?org_id={org_id}" if org_id else "/admin/skills"


def _back(base: str, msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg={msg}", status_code=303)


@router.post("/admin/skills/create")
async def admin_skill_create(
    request: Request,
    org_id: int = Query(0),
    name: str = Form(...),
    description: str = Form(""),
    room_types: List[int] = Form([]),
    keywords: str = Form(""),
    patterns: str = Form(""),
    room_keywords: str = Form(""),
    speaker_keywords: str = Form(""),
    content: str = Form(...),
):
    try:
        trg = _build_trigger(room_types, keywords, patterns, room_keywords, speaker_keywords)
        await skill_service.create_skill(name, description, trg, content)
        return _back(_skill_base(org_id), "✅ 技能已创建（在智能体管理中勾选启用）")
    except Exception as e:
        return _back(_skill_base(org_id), f"❌ {esc(str(e))}")


@router.post("/admin/skills/{skill_id}/update")
async def admin_skill_update(
    request: Request,
    skill_id: int,
    org_id: int = Query(0),
    name: str = Form(...),
    description: str = Form(""),
    room_types: List[int] = Form([]),
    keywords: str = Form(""),
    patterns: str = Form(""),
    room_keywords: str = Form(""),
    speaker_keywords: str = Form(""),
    content: str = Form(...),
):
    try:
        trg = _build_trigger(room_types, keywords, patterns, room_keywords, speaker_keywords)
        ok = await skill_service.update_skill(skill_id, name, description, trg, content)
        if not ok:
            raise ValueError(f"技能 #{skill_id} 不存在（可能已被删除）")
        return _back(_skill_base(org_id), f"✅ 技能 #{skill_id} 已更新")
    except Exception as e:
        # 校验失败回列表页时带上 skill_id，表单保持编辑态，填的内容不白填
        return _back(f"{_skill_base(org_id)}{'&' if '?' in _skill_base(org_id) else '?'}skill_id={skill_id}",
                     f"❌ {esc(str(e))}")


@router.post("/admin/skills/{skill_id}/toggle")
async def admin_skill_toggle(skill_id: int, org_id: int = Query(0)):
    skills = await skill_service.list_skills()
    cur = next((s for s in skills if s["id"] == skill_id), None)
    if cur:
        await skill_service.set_enabled(skill_id, not cur["enabled"])
    return RedirectResponse(url=_skill_base(org_id), status_code=303)


@router.get("/admin/skills/{skill_id}/delete")
async def admin_skill_delete(skill_id: int, org_id: int = Query(0)):
    await skill_service.delete_skill(skill_id)
    return _back(_skill_base(org_id), "✅ 技能已删除")
