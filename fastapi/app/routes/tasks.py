"""
任务模块路由：REST API + 管理页面（/admin/tasks）

任务 = 触发器 × 动作
- 触发器：schedule（cron5 / interval）| condition（friend_accepted / message_from）
- 动作：workflow（执行工作流）| agent（向智能体注入指令）
"""
import json
import logging
import time
from datetime import datetime
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..models.task import (
    ACTION_AGENT,
    ACTION_WORKFLOW,
    COND_FRIEND_ACCEPTED,
    COND_MESSAGE_FROM,
    TRIGGER_CONDITION,
    TRIGGER_SCHEDULE,
    TaskCreateRequest,
    TaskUpdateRequest,
)
from ..services import (
    agent_service,
    group_service,
    robot_service,
    task_service,
    workflow_service,
)
from ..services.task_service import build_cron_from_form, parse_datetime_local
from ..layout import page, esc

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])


# ── 展示辅助 ──────────────────────────────────────

def _trigger_summary(t: dict) -> str:
    try:
        spec = json.loads(t["trigger_spec"] or "{}")
    except Exception:
        spec = {}
    if t["trigger_type"] == TRIGGER_SCHEDULE:
        if spec.get("once_at"):
            return f"单次：{_fmt_ts(int(spec['once_at']))}"
        if spec.get("cron"):
            s = f"cron <code>{esc(spec['cron'])}</code>"
            if spec.get("repeat_count"):
                s += f"（共 {int(spec['repeat_count'])} 次）"
            return s
        if spec.get("interval_sec"):
            return f"每 {int(spec['interval_sec'])} 秒"
        return "定时"
    ctype = spec.get("type", "?")
    if ctype == COND_FRIEND_ACCEPTED:
        tg = spec.get("target") or {}
        parts = []
        if tg.get("phone"):
            parts.append(f"手机号 {esc(tg['phone'])}")
        if tg.get("name"):
            parts.append(f"昵称 {esc(tg['name'])}")
        return f"好友通过（{'、'.join(parts) or '?'}）"
    if ctype == COND_MESSAGE_FROM:
        return f"收到消息：{esc(str(spec.get('target', '?')))}"
    return esc(ctype)


def _action_summary(t: dict) -> str:
    try:
        spec = json.loads(t["action_spec"] or "{}")
    except Exception:
        spec = {}
    if t["action_type"] == ACTION_WORKFLOW:
        return f"工作流 #{spec.get('workflow_id')}"
    if t["action_type"] == ACTION_AGENT:
        ins = str(spec.get("instruction") or "")
        ins = ins[:24] + "…" if len(ins) > 24 else ins
        return f"智能体 #{spec.get('agent_id')}：{esc(ins)}"
    return t["action_type"]


def _state_badge(t: dict) -> str:
    if not t["enabled"]:
        return '<span class="badge badge-offline">已停用</span>'
    if t["state"] == "waiting":
        return '<span class="badge badge-blue">等待条件</span>'
    if t["state"] == "running":
        return '<span class="badge badge-online">执行中</span>'
    if t["state"] == "done":
        return '<span class="badge badge-offline">已完成</span>'
    return '<span class="badge">待触发</span>'


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "-"


def _oid_q(org_id: int) -> str:
    return f"?org_id={org_id}" if org_id else ""


# ── REST API ─────────────────────────────────────

@router.get("/api/tasks")
async def api_tasks(org_id: int = 0):
    """任务列表"""
    try:
        tasks = await task_service.list_tasks(org_id)
        return {"ok": True, "data": tasks}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/tasks")
async def api_task_create(req: TaskCreateRequest):
    """创建任务"""
    try:
        ok, res = await task_service.create_task(req)
        if not ok:
            return JSONResponse({"ok": False, "error": res}, status_code=400)
        return {"ok": True, "data": {"id": res}}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.put("/api/tasks/{task_id}")
async def api_task_update(task_id: int, req: TaskUpdateRequest):
    try:
        ok, err = await task_service.update_task(task_id, req)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/tasks/{task_id}/toggle")
async def api_task_toggle(task_id: int):
    try:
        ok = await task_service.toggle_task(task_id)
        return {"ok": ok} if ok else JSONResponse({"ok": False, "error": "任务不存在"}, status_code=404)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/tasks/{task_id}/trigger")
async def api_task_trigger(task_id: int):
    """手动触发一次（测试用）"""
    try:
        ok, err = await task_service.trigger_now(task_id)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.delete("/api/tasks/{task_id}")
async def api_task_delete(task_id: int):
    try:
        await task_service.delete_task(task_id)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/tasks/{task_id}/runs")
async def api_task_runs(task_id: int, limit: int = 50):
    try:
        runs = await task_service.list_runs(task_id, limit)
        return {"ok": True, "data": runs}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── 管理页面 ─────────────────────────────────────

@router.get("/admin/tasks", response_class=HTMLResponse)
async def admin_tasks(msg: str = "", org_id: int = 0):
    """任务列表页 + 新建表单"""
    tasks = await task_service.list_tasks(org_id)
    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if not grp:
            return RedirectResponse(url="/admin/orgs?msg=❌ 组织不存在", status_code=303)
        cur_org_name = grp["name"]

    robots = await robot_service.list_robots()
    robot_opts = "".join(
        f'<option value="{esc(r.robot_id)}">{esc(r.device_name or r.robot_id)}</option>'
        for r in robots
    )
    wfs = await workflow_service.list_workflows()
    wf_opts = "".join(
        f'<option value="{w.id}">#{w.id} {esc(w.name)}</option>' for w in wfs
    )
    agents = await agent_service.list_agents()
    agent_opts = "".join(
        f'<option value="{a.id}">#{a.id} {esc(a.name)}</option>' for a in agents
    )

    rows = ""
    if tasks:
        for t in tasks:
            oid = _oid_q(org_id)
            last = "✅" if t["last_result"] == "success" else (
                "❌" if t["last_result"] == "failed" else "-"
            )
            rows += (
                f'<tr><td>#{t["id"]}</td>'
                f'<td><b>{esc(t["name"])}</b></td>'
                f'<td>{_trigger_summary(t)}</td>'
                f'<td>{_action_summary(t)}</td>'
                f'<td>{esc(t["robot_id"] or "-")}</td>'
                f'<td>{_state_badge(t)}</td>'
                f'<td>{t["run_count"]}</td>'
                f'<td>{last} {_fmt_ts(t["last_run_at"])}</td>'
                f'<td class="row-actions">'
                f'<a href="/admin/task/{t["id"]}{oid}" class="btn btn-ghost btn-sm">详情</a> '
                f'<a href="/admin/task/{t["id"]}/trigger{oid}" class="btn btn-outline btn-sm" '
                f'onclick="return confirm(\'手动触发一次？\')">触发</a> '
                f'<a href="/admin/task/{t["id"]}/toggle{oid}" class="btn btn-outline btn-sm">'
                f'{"停用" if t["enabled"] else "启用"}</a> '
                f'<a href="/admin/task/{t["id"]}/delete{oid}" class="btn btn-danger btn-sm" '
                f'onclick="return confirm(\'删除任务及其执行历史？\')">删除</a></td></tr>'
            )
        table = (
            '<div class="table-wrap"><table class="table"><thead><tr>'
            "<th>ID</th><th>名称</th><th>触发器</th><th>动作</th><th>设备</th>"
            "<th>状态</th><th>次数</th><th>上次</th><th>操作</th>"
            "</tr></thead><tbody>" + rows + "</tbody></table></div>"
        )
    else:
        table = '<div class="empty">还没有任务。创建一个定时任务或条件任务，条件满足时自动执行工作流 / 向智能体发布指令。</div>'

    oid = _oid_q(org_id)
    form = f"""
    <details class="card" style="margin-bottom:18px">
      <summary style="cursor:pointer;padding:14px 18px;font-weight:600">＋ 新建任务</summary>
      <form method="POST" action="/admin/task/create{oid}" style="padding:0 18px 18px">
        <div style="margin:0 0 6px;font-weight:600;color:#0f172a">① 基本信息</div>
        <div class="form-row" style="align-items:flex-start">
          <div class="form-group" style="flex:2"><label>任务名称 *</label>
            <input type="text" name="name" required placeholder="如：新好友欢迎语" style="width:100%"></div>
          <div class="form-group"><label>绑定设备</label>
            <select name="robot_id" style="min-width:180px"><option value="">（不指定）</option>{robot_opts}</select></div>
        </div>
        <div style="margin:16px 0 6px;font-weight:600;color:#0f172a;border-top:1px solid #e2e8f0;padding-top:12px">② 触发器</div>
        <div class="form-group">
          <label>触发器类型</label>
          <div class="form-row" style="gap:18px">
            <label class="form-check"><input type="radio" name="trigger_type" value="schedule" checked onchange="tgSwitch(this)"> 定时触发</label>
            <label class="form-check"><input type="radio" name="trigger_type" value="condition" onchange="tgSwitch(this)"> 条件触发</label>
          </div>
        </div>
        <div class="form-group" id="tg-schedule" style="margin-top:8px">
          <div class="form-row" style="gap:12px;align-items:flex-end">
            <div class="form-group">
              <label>执行方式</label>
              <select name="trigger_mode" onchange="modeSwitch(this)">
                <option value="repeat">重复执行</option>
                <option value="once">单次执行</option>
              </select>
            </div>
          </div>
          <div id="mode-repeat" style="margin-top:8px">
            <div class="form-group">
              <label>重复方式</label>
              <select name="repeat_method" onchange="rmSwitch(this)">
                <option value="cron" selected>定时计划（cron）</option>
                <option value="interval">固定间隔</option>
              </select>
            </div>
            <div id="repeat-method-cron" style="margin-top:8px">
              <div class="form-group">
                <label>定义方式</label>
                <select name="cron_def" onchange="cdSwitch(this)">
                  <option value="selector" selected>选择器（执行时间+频率）</option>
                  <option value="format">cron 表达式</option>
                </select>
              </div>
              <div id="cron-def-selector" style="margin-top:8px">
                <div class="form-row" style="gap:12px;align-items:flex-end">
                  <div class="form-group">
                    <label>执行时间 *</label>
                    <input type="time" name="trigger_time" value="09:00" required>
                  </div>
                  <div class="form-group">
                    <label>重复频率</label>
                    <select name="trigger_freq" onchange="freqSwitch(this)">
                      <option value="daily">每天</option>
                      <option value="weekly">每周</option>
                      <option value="monthly">每月</option>
                    </select>
                  </div>
                </div>
                <div id="freq-weekdays" style="display:none;margin-top:8px">
                  <span class="form-hint">选择星期（可多选）：</span>
                  <div>
                    <label class="form-check"><input type="checkbox" name="trigger_weekdays" value="0"> 周日</label>
                    <label class="form-check"><input type="checkbox" name="trigger_weekdays" value="1"> 周一</label>
                    <label class="form-check"><input type="checkbox" name="trigger_weekdays" value="2"> 周二</label>
                    <label class="form-check"><input type="checkbox" name="trigger_weekdays" value="3"> 周三</label>
                    <label class="form-check"><input type="checkbox" name="trigger_weekdays" value="4"> 周四</label>
                    <label class="form-check"><input type="checkbox" name="trigger_weekdays" value="5"> 周五</label>
                    <label class="form-check"><input type="checkbox" name="trigger_weekdays" value="6"> 周六</label>
                  </div>
                </div>
                <div id="freq-monthly" style="display:none;margin-top:8px">
                  <span class="form-hint">每月</span>
                  <input type="number" name="trigger_monthday" min="1" max="31" value="1" style="width:70px">
                  <span class="form-hint">号</span>
                </div>
              </div>
              <div id="cron-def-format" style="display:none;margin-top:8px">
                <label>cron 表达式 *（5 段：分 时 日 月 周）</label>
                <input type="text" name="cron" placeholder="如 0 9 * * *" style="width:300px">
                <div class="form-hint">示例：0 9 * * * = 每天 09:00；0 9 * * 1 = 每周一 09:00；*/15 * * * * = 每 15 分钟</div>
              </div>
            </div>
            <div id="repeat-method-interval" style="display:none;margin-top:8px">
              <label>间隔秒数 *</label>
              <input type="number" name="interval_sec" min="1" placeholder="如 3600" style="width:160px">
              <div class="form-hint">每 N 秒触发一次。3600 = 每小时，86400 = 每天</div>
            </div>
            <div class="form-row" style="gap:12px;align-items:flex-end;margin-top:8px">
              <div class="form-group">
                <label>重复次数（0=无限）</label>
                <input type="number" name="repeat_count" min="0" value="0" style="width:110px">
              </div>
            </div>
            <div class="form-hint" style="margin-top:4px">执行满 N 次后任务自动结束（两种重复方式通用）</div>
          </div>
          <div id="mode-once" style="display:none;margin-top:8px">
            <label>执行时间（年-月-日 时:分）*</label>
            <input type="datetime-local" name="once_at" step="60" required>
            <div class="form-hint">只执行一次，执行后任务自动结束</div>
          </div>
        </div>
        <div class="form-group" id="tg-condition" style="display:none;margin-top:8px">
          <div class="form-row" style="gap:12px;align-items:flex-end">
            <div class="form-group">
              <label>条件类型</label>
              <select name="cond_type" onchange="condSwitch(this)">
                <option value="friend_accepted">好友申请通过</option>
                <option value="message_from">收到某人消息</option>
              </select>
            </div>
            <div class="form-group">
              <label>目标（任一命中即触发）</label>
              <div class="form-row" style="gap:6px">
                <input type="text" name="target_name" placeholder="昵称 / 备注（选填）" style="width:180px">
              </div>
              <div class="form-hint" id="cond-fa">好友申请通过：按昵称 / 备注匹配（企业微信显示名，填备注或昵称均可）</div>
              <div class="form-hint" id="cond-mf" style="display:none">收到某人消息：按会话名匹配（昵称或备注）</div>
            </div>
            <div class="form-group">
              <label>过期时间（小时）</label>
              <input type="number" name="expires_hours" value="0" style="width:80px">
              <div class="form-hint">0=不过期</div>
            </div>
          </div>
        </div>
        <div style="margin:16px 0 6px;font-weight:600;color:#0f172a;border-top:1px solid #e2e8f0;padding-top:12px">③ 动作</div>
        <div class="form-group">
          <label>动作类型</label>
          <div class="form-row" style="gap:18px">
            <label class="form-check"><input type="radio" name="action_type" value="workflow" checked onchange="acSwitch(this)"> 执行工作流</label>
            <label class="form-check"><input type="radio" name="action_type" value="agent" onchange="acSwitch(this)"> 注入智能体指令</label>
          </div>
        </div>
        <div class="form-group" id="ac-workflow" style="margin-top:8px">
          <label>工作流 *</label>
          <select name="workflow_id" style="min-width:220px">{wf_opts}</select>
          <div class="form-hint">参数（JSON，选填）：</div>
          <textarea name="wf_params" rows="2" style="width:100%" placeholder='{{"text": "..."}}'></textarea>
        </div>
        <div class="form-group" id="ac-agent" style="display:none;margin-top:8px">
          <label>智能体 *</label>
          <select name="agent_id" style="min-width:200px">{agent_opts}</select>
          <div class="form-hint">注入指令（发给智能体处理）：</div>
          <textarea name="instruction" rows="2" style="width:100%" placeholder="如：新好友 {{friend.name}} 已通过申请，请发送欢迎语"></textarea>
          <div class="form-row" style="gap:10px;align-items:center;margin-top:6px">
            <label class="form-check" style="margin-right:2px">让智能体回复</label>
            <select name="send_reply" id="send_reply" onchange="srSwitch(this)" style="width:120px">
              <option value="none" selected>不发送</option>
              <option value="source">来源会话</option>
              <option value="custom">自定义会话</option>
            </select>
            <input type="text" name="room" id="sr-room" placeholder="回复会话名（好友昵称/群名）" style="width:200px;display:none">
          </div>
          <div class="form-hint" style="margin-top:4px">回复由智能体自己通过设备指令 <code>203 发送消息</code> 发出（服务器不再代发其文本输出，智能体须已获授权 203）。「来源会话」= 触发事件所在会话（条件任务=好友昵称/群名；定时任务无来源时等同不发送）；「自定义会话」= 回复到下方填写的会话名</div>
        </div>
        <div style="margin-top:14px">
          <label class="form-check"><input type="checkbox" name="enabled" checked> 创建后立即启用</label>
          <button class="btn btn-primary" style="margin-left:14px">创建任务</button>
        </div>
      </form>
    </details>
    <script>
    function tgSwitch(radio){{
      var c = radio.value === 'condition';
      document.getElementById('tg-schedule').style.display = c ? 'none' : '';
      document.getElementById('tg-condition').style.display = c ? '' : 'none';
    }}
    function freqSwitch(sel){{
      document.getElementById('freq-weekdays').style.display = sel.value === 'weekly' ? '' : 'none';
      document.getElementById('freq-monthly').style.display = sel.value === 'monthly' ? '' : 'none';
    }}
    function modeSwitch(sel){{
      var rep = document.getElementById('mode-repeat');
      var once = document.getElementById('mode-once');
      var isOnce = sel.value === 'once';
      rep.style.display = isOnce ? 'none' : '';
      once.style.display = isOnce ? '' : 'none';
      [rep, once].forEach(function(g){{
        g.querySelectorAll('input,select').forEach(function(c){{ c.disabled = g.style.display === 'none'; }});
      }});
    }}
    function rmSwitch(sel){{
      var cron = document.getElementById('repeat-method-cron');
      var interval = document.getElementById('repeat-method-interval');
      var isInterval = sel.value === 'interval';
      cron.style.display = isInterval ? 'none' : '';
      interval.style.display = isInterval ? '' : 'none';
      [cron, interval].forEach(function(g){{
        g.querySelectorAll('input,select').forEach(function(c){{ c.disabled = g.style.display === 'none'; }});
      }});
    }}
    function cdSwitch(sel){{
      var selG = document.getElementById('cron-def-selector');
      var fmtG = document.getElementById('cron-def-format');
      var isFormat = sel.value === 'format';
      selG.style.display = isFormat ? 'none' : '';
      fmtG.style.display = isFormat ? '' : 'none';
      [selG, fmtG].forEach(function(g){{
        g.querySelectorAll('input,select').forEach(function(c){{ c.disabled = g.style.display === 'none'; }});
      }});
    }}
    function condSwitch(sel){{
      var fa = sel.value === 'friend_accepted';
      document.getElementById('cond-fa').style.display = fa ? '' : 'none';
      document.getElementById('cond-mf').style.display = fa ? 'none' : '';
    }}
    function acSwitch(radio){{
      var w = radio.value === 'workflow';
      document.getElementById('ac-workflow').style.display = w ? '' : 'none';
      document.getElementById('ac-agent').style.display = w ? 'none' : '';
    }}
    function srSwitch(sel){{
      var custom = sel.value === 'custom';
      var box = document.getElementById('sr-room');
      box.style.display = custom ? '' : 'none';
      box.disabled = !custom;
    }}
    (function(){{
      var t = document.querySelector('select[name="trigger_mode"]'); if (t) modeSwitch(t);
      var r = document.querySelector('select[name="repeat_method"]'); if (r) rmSwitch(r);
      var c = document.querySelector('select[name="cron_def"]'); if (c) cdSwitch(c);
      var f = document.querySelector('select[name="trigger_freq"]'); if (f) freqSwitch(f);
      var s = document.querySelector('select[name="send_reply"]'); if (s) srSwitch(s);
    }})();
    </script>
    """

    content = form + table
    return page(
        "任务管理 - Aivbot",
        "📋 任务管理",
        content,
        msg=msg,
        subtitle="定时 / 条件触发，动作=工作流或智能体指令",
        active="tasks",
        org_id=org_id,
        org_name=cur_org_name,
        org_active="tasks",
    )


@router.post("/admin/task/create")
async def admin_task_create(
    name: str = Form(...),
    robot_id: str = Form(""),
    trigger_type: str = Form(TRIGGER_SCHEDULE),
    trigger_mode: str = Form("repeat"),
    repeat_method: str = Form("cron"),
    cron_def: str = Form("selector"),
    trigger_time: str = Form(""),
    trigger_freq: str = Form("daily"),
    trigger_weekdays: List[int] = Form([]),
    trigger_monthday: int = Form(1),
    repeat_count: int = Form(0),
    once_at: str = Form(""),
    cron: str = Form(""),
    interval_sec: str = Form(""),
    cond_type: str = Form(COND_FRIEND_ACCEPTED),
    target_phone: str = Form(""),
    target_name: str = Form(""),
    expires_hours: str = Form("0"),
    action_type: str = Form(ACTION_WORKFLOW),
    workflow_id: int = Form(0),
    wf_params: str = Form(""),
    agent_id: int = Form(0),
    instruction: str = Form(""),
    send_reply: str = Form("none"),
    room: str = Form(""),
    room_type: int = Form(1),
    enabled: str = Form(""),
    org_id: int = Query(0),
):
    """新建任务（表单）

    org_id 来自 URL 查询串（表单 action 拼 ?org_id=N），与智能体/工作流创建保持一致；
    在组织上下文内创建的任务即归属该组织（tasks.org_id 列，列表/API 按此过滤）。
    """
    oid = _oid_q(org_id)

    # 触发器
    if trigger_type == TRIGGER_SCHEDULE:
        if trigger_mode == "once":
            once_ts = parse_datetime_local(once_at)
            if once_ts is None or once_ts <= int(time.time()):
                return RedirectResponse(
                    url=f"/admin/tasks{oid}{'&' if oid else '?'}msg=❌ 单次执行时间必须晚于当前时间"
                    if org_id else f"/admin/tasks?msg=❌ 单次执行时间必须晚于当前时间",
                    status_code=303,
                )
            spec = {"once_at": once_ts}
        else:
            # 重复执行：重复方式二选一（定时计划 cron / 固定间隔）
            if repeat_method == "interval":
                try:
                    sec = int(interval_sec)
                    if sec <= 0:
                        raise ValueError
                except Exception:
                    return RedirectResponse(
                        url=f"/admin/tasks{oid}{'&' if oid else '?'}msg=❌ 请填写间隔秒数（正数）"
                        if org_id else f"/admin/tasks?msg=❌ 请填写间隔秒数（正数）",
                        status_code=303,
                    )
                spec = {"interval_sec": sec}
            else:
                # 定时计划（cron）：定义方式二选一（选择器 / cron 表达式）
                if cron_def == "format":
                    cron_expr = cron.strip()
                    if not cron_expr:
                        return RedirectResponse(
                            url=f"/admin/tasks{oid}{'&' if oid else '?'}msg=❌ 请填写 cron 表达式"
                            if org_id else f"/admin/tasks?msg=❌ 请填写 cron 表达式",
                            status_code=303,
                        )
                    spec = {"cron": cron_expr}
                else:
                    c = build_cron_from_form(
                        trigger_time, trigger_freq, trigger_weekdays, trigger_monthday
                    )
                    if not c:
                        return RedirectResponse(
                            url=f"/admin/tasks{oid}{'&' if oid else '?'}msg=❌ 请选择执行时间"
                            if org_id else f"/admin/tasks?msg=❌ 请选择执行时间",
                            status_code=303,
                        )
                    spec = {"cron": c}
            # 重复次数（0=无限）对两种重复方式通用
            if repeat_count > 0:
                spec["repeat_count"] = repeat_count
    else:
        spec = {"type": cond_type, "oneshot": True}
        if cond_type == COND_FRIEND_ACCEPTED:
            target = {}
            if target_phone.strip():
                target["phone"] = target_phone.strip()
            if target_name.strip():
                target["name"] = target_name.strip()
            if not target:
                return RedirectResponse(
                    url=f"/admin/tasks{oid}{'&' if oid else '?'}msg=❌ 好友申请条件需填写昵称/备注"
                    if org_id else "/admin/tasks?msg=❌ 好友申请条件需填写昵称/备注",
                    status_code=303,
                )
            spec["target"] = target
        else:
            if not target_name.strip():
                return RedirectResponse(
                    url=f"/admin/tasks{oid}{'&' if oid else '?'}msg=❌ 收到消息条件需填写对方昵称/备注"
                    if org_id else "/admin/tasks?msg=❌ 收到消息条件需填写对方昵称/备注",
                    status_code=303,
                )
            spec["target"] = target_name.strip()

    # 动作
    if action_type == ACTION_WORKFLOW:
        aspec: dict = {"workflow_id": workflow_id}
        if wf_params.strip():
            try:
                aspec["params"] = json.loads(wf_params)
            except Exception:
                return RedirectResponse(
                    url=f"/admin/tasks{oid}{'&' if oid else '?'}msg=❌ 工作流参数 JSON 无效"
                    if org_id else "/admin/tasks?msg=❌ 工作流参数 JSON 无效",
                    status_code=303,
                )
    else:
        # 发送回复三态：none=不发送 / source=回复到触发事件会话 / custom=回复到指定会话
        send_mode = send_reply.strip().lower()
        if send_mode not in ("none", "source", "custom"):
            send_mode = "none"
        if send_mode == "custom" and not room.strip():
            return RedirectResponse(
                url=f"/admin/tasks{oid}{'&' if oid else '?'}msg=❌ 「自定义会话」需填写回复会话名"
                if org_id else "/admin/tasks?msg=❌ 「自定义会话」需填写回复会话名",
                status_code=303,
            )
        aspec = {
            "agent_id": agent_id,
            "instruction": instruction.strip(),
            "send_reply": send_mode,
            "room": room.strip(),
            "room_type": room_type,
        }

    req = TaskCreateRequest(
        name=name.strip(),
        org_id=org_id,
        robot_id=robot_id,
        trigger_type=trigger_type,
        trigger_spec=spec,
        action_type=action_type,
        action_spec=aspec,
        enabled=enabled == "on",
        expires_at=int(time.time()) + int(expires_hours or 0) * 3600
        if trigger_type == TRIGGER_CONDITION and expires_hours.strip() and int(expires_hours or 0) > 0
        else 0,
    )
    ok, res = await task_service.create_task(req)
    if not ok:
        msg = f"❌ {res}"
    else:
        msg = f"✅ 已创建任务 #{res}"
    # msg 含 # 等字符必须 URL 编码，否则 # 后内容（含 &org_id=）会被当锚点截断
    return RedirectResponse(
        url=f"/admin/tasks?msg={quote(msg)}{('&org_id=' + str(org_id)) if org_id else ''}",
        status_code=303,
    )


@router.get("/admin/task/{task_id}", response_class=HTMLResponse)
async def admin_task_detail(task_id: int, msg: str = "", org_id: int = 0):
    """任务详情 + 执行历史"""
    t = await task_service.get_task(task_id)
    if not t:
        return RedirectResponse(url="/admin/tasks?msg=❌ 任务不存在", status_code=303)
    runs = await task_service.list_runs(task_id, 50)
    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if grp:
            cur_org_name = grp["name"]

    run_rows = ""
    if runs:
        for r in runs:
            try:
                res = json.loads(r["result"] or "{}")
                brief = json.dumps(res, ensure_ascii=False)[:160]
            except Exception:
                brief = ""
            status_badge = (
                '<span class="badge badge-online">成功</span>' if r["status"] == "success"
                else '<span class="badge badge-warn">失败</span>' if r["status"] == "failed"
                else '<span class="badge badge-blue">执行中</span>'
            )
            run_rows += (
                f'<tr><td>#{r["id"]}</td><td>{esc(r["trigger_desc"])}</td>'
                f'<td>{status_badge}</td>'
                f'<td>{_fmt_ts(r["started_at"])}</td>'
                f'<td><code>{esc(brief)}</code></td></tr>'
            )
        runs_html = (
            '<div class="table-wrap"><table class="table"><thead><tr>'
            "<th>#</th><th>触发</th><th>状态</th><th>开始</th><th>结果</th>"
            "</tr></thead><tbody>" + run_rows + "</tbody></table></div>"
        )
    else:
        runs_html = '<div class="empty">暂无执行记录。</div>'

    oid = _oid_q(org_id)
    info = (
        f'<div class="card"><div class="card-hd"><h3>#{t["id"]} {esc(t["name"])}</h3>{_state_badge(t)}</div>'
        f'<p class="card-sub">'
        f'触发器：{_trigger_summary(t)}<br>动作：{_action_summary(t)}<br>'
        f'设备：{esc(t["robot_id"] or "-")}｜执行次数：{t["run_count"]}｜'
        f'上次：{_fmt_ts(t["last_run_at"])}'
        f'</p>'
        f'<a href="/admin/task/{task_id}/trigger{oid}" class="btn btn-success btn-sm" onclick="return confirm(\'手动触发一次？\')">▶ 手动触发</a> '
        f'<a href="/admin/task/{task_id}/toggle{oid}" class="btn btn-outline btn-sm">{("停用" if t["enabled"] else "启用")}</a> '
        f'<a href="/admin/tasks{oid}" class="btn btn-ghost btn-sm">← 返回列表</a></div>'
    )

    content = info + f"<h3 style=\"margin:18px 0 10px\">执行历史</h3>" + runs_html
    return page(
        f"任务 #{task_id} - Aivbot",
        f"📋 任务 #{task_id}",
        content,
        msg=msg,
        active="tasks",
        org_id=org_id,
        org_name=cur_org_name,
        org_active="tasks",
    )


@router.get("/admin/task/{task_id}/toggle")
async def admin_task_toggle(task_id: int, org_id: int = 0):
    ok = await task_service.toggle_task(task_id)
    msg = "✅ 已启用" if ok else "❌ 任务不存在"
    if ok:
        t = await task_service.get_task(task_id)
        msg = "✅ 已启用" if t and t["enabled"] else "✅ 已停用"
    return RedirectResponse(
        url=f"/admin/tasks?msg={quote(msg)}{('&org_id=' + str(org_id)) if org_id else ''}",
        status_code=303,
    )


@router.get("/admin/task/{task_id}/delete")
async def admin_task_delete(task_id: int, org_id: int = 0):
    await task_service.delete_task(task_id)
    return RedirectResponse(
        url=f"/admin/tasks?msg={quote('✅ 已删除任务 #' + str(task_id))}{('&org_id=' + str(org_id)) if org_id else ''}",
        status_code=303,
    )


@router.get("/admin/task/{task_id}/trigger")
async def admin_task_trigger(task_id: int, org_id: int = 0):
    ok, err = await task_service.trigger_now(task_id)
    msg = "✅ 已触发" if ok else f"❌ {err}"
    return RedirectResponse(
        url=f"/admin/tasks?msg={quote(msg)}{('&org_id=' + str(org_id)) if org_id else ''}",
        status_code=303,
    )
