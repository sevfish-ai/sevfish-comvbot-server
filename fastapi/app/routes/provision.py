"""
「AI 配置组织」——组织配置智能体（agents.kind='provisioner'）的配置页与对话配置台。

管理员在这里单独给配置智能体配 LLM 与提示词；配置台里描述需求，它直接调用
provision_* 工具创建组织及其配套资源（智能体/表格/工作流/知识库/任务/设备）。
"""

import json
import logging

from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse

from ..layout import esc, page
from ..models.agent import AgentCreateRequest
from ..services import agent_service
from ..services.provision_service import (
    DEFAULT_PROMPT,
    PROVISIONER_DESC,
    PROVISIONER_NAME,
    TOOL_DEFINITIONS,
)
from .agents import (
    SWITCH_CSS,
    SWITCH_JS,
    _switch_html,
    provider_model_fields,
    provider_model_js,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["provision"])

# 配置台会话的房间名。合成设备号用 test{agent_id}，这样「清空对话」能直接复用
# agent_service.clear_test_conversations（它按 robot_id LIKE 'test%' 或房间名匹配）。
CONSOLE_ROOM = "组织配置台"


async def _provisioner():
    """确保配置智能体存在（不存在则按默认提示词创建）"""
    return await agent_service.ensure_provisioner(
        PROVISIONER_NAME, PROVISIONER_DESC, DEFAULT_PROMPT
    )


def _status_chips(agent) -> str:
    llm_ok = bool(agent.model and agent.api_key)
    key = "已配置" if agent.api_key else "未配置"
    enabled = "启用" if agent.enabled else "停用"
    chips = [
        f'<span class="stat-chip">🧠 {esc(agent.model or "未选模型")}</span>',
        f'<span class="stat-chip">🔑 API Key {key}</span>',
        f'<span class="stat-chip">🧰 {len(TOOL_DEFINITIONS)} 个配置工具</span>',
        f'<span class="stat-chip">⚡ {enabled}</span>',
    ]
    if not llm_ok:
        chips.append(
            '<span class="stat-chip" style="background:#fee2e2;color:#b91c1c">'
            "⚠ 先填 LLM 配置，否则配置台无法工作</span>"
        )
    return f'<div class="inline-flex" style="gap:6px;flex-wrap:wrap">{"".join(chips)}</div>'


def _tool_list_html() -> str:
    rows = "".join(
        f"<tr><td><code>{esc(t['function']['name'])}</code></td>"
        f"<td>{esc(t['function']['description'])}</td>"
        f"<td class='mono' style='font-size:12px'>"
        f"{esc('、'.join(t['function']['parameters'].get('required') or []) or '—')}</td></tr>"
        for t in TOOL_DEFINITIONS
    )
    return (
        '<div class="card"><details><summary style="cursor:pointer">'
        f"🧰 它能调用的工具（{len(TOOL_DEFINITIONS)} 个，全部为「创建/追加」，没有删除能力）"
        "</summary>"
        '<table class="table" style="margin-top:10px"><thead><tr>'
        "<th>工具</th><th>作用</th><th>必填参数</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></details></div>"
    )


# ── 配置页 ──────────────────────────────────────────────────

@router.get("/admin/orgs/provisioner")
async def admin_provisioner(msg: str = ""):
    agent = await _provisioner()
    content = f"""
    {SWITCH_CSS}
    <div class="card">
        <h3>🤖 AI 配置组织</h3>
        <p class="card-sub">
            管理员直接向这个智能体描述需求，它就在库里创建出组织及其配套资源
            （智能体 / 表格 / 工作流 / 知识库 / 任务 / 设备 / 客户账号）。<br>
            它是系统级工具：不属于任何组织，也不会出现在设备绑定、任务、工作流等下拉里；
            下面单独给它配 LLM 与提示词。
        </p>
        {_status_chips(agent)}
        <div style="margin-top:12px" class="inline-flex">
            <a class="btn btn-primary" href="/admin/orgs/provisioner/chat">💬 打开配置台</a>
            <a class="btn" href="/admin/orgs" style="background:#f1f2f6;color:#374151;border:1px solid #e5e7eb">← 返回组织管理</a>
        </div>
    </div>

    <form method="POST" action="/admin/orgs/provisioner/save" class="card form-stack">
        <h3>🔧 LLM 配置</h3>
        <p class="card-sub">与业务智能体同一套 OpenAI 兼容接口。新建的组织智能体默认继承这里的配置。</p>
        <div class="form-row">
            {provider_model_fields(agent.provider, agent.base_url, agent.model, required=False)}
        </div>
        <div class="form-row">
            <div class="form-group" style="flex:1"><label>API Key</label>
                <input type="password" name="api_key" value=""
                       placeholder="{'留空表示保留原 Key' if agent.api_key else '必填'}"></div>
            <div class="form-group" style="flex:0 0 120px"><label>temperature</label>
                <input type="number" step="0.1" min="0" max="2" name="temperature" value="{agent.temperature}"></div>
            <div class="form-group" style="flex:0 0 140px"><label>max_tokens</label>
                <input type="number" min="256" max="32768" name="max_tokens" value="{agent.max_tokens}"></div>
        </div>
        <div class="form-group">
            <label>系统提示词（决定它怎么理解需求、建哪些资源）</label>
            <textarea name="system_prompt" rows="20" class="mono" style="font-size:12.5px">{esc(agent.system_prompt)}</textarea>
        </div>
        <div class="form-group form-switch-row">
            <label class="mb-0">启用（停用后配置台不可用）</label>
            {_switch_html("enabled", agent.enabled, "启用", "停用")}
        </div>
        <div><button class="btn btn-primary">💾 保存配置</button></div>
    </form>

    <div class="card">
        <h3>♻️ 提示词</h3>
        <p class="card-sub">默认提示词随代码走，只在智能体首次创建时写入。改坏了可以一键恢复默认。</p>
        <form method="POST" action="/admin/orgs/provisioner/reset-prompt"
              onsubmit="return confirm('用默认提示词覆盖当前提示词？')">
            <button class="btn" style="background:#f1f2f6;color:#b91c1c;border:1px solid #e5e7eb">↩️ 恢复默认提示词</button>
        </form>
    </div>
    {_tool_list_html()}
    {SWITCH_JS}
    {provider_model_js()}
    """
    return page("AI 配置组织", "🤖 AI 配置组织", content, msg, bare=True)


@router.post("/admin/orgs/provisioner/save")
async def admin_provisioner_save(
    provider: str = Form("custom"),
    base_url: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
    temperature: float = Form(0.7),
    max_tokens: int = Form(8192),
    system_prompt: str = Form(""),
    enabled: str = Form(""),
):
    agent = await _provisioner()
    try:
        await agent_service.update_agent(
            agent.id,
            AgentCreateRequest(
                name=agent.name,
                description=agent.description,
                provider=provider,
                base_url=base_url,
                model=model,
                api_key=api_key,          # 留空 = 保留原 Key（update_agent 内处理）
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                max_context_turns=agent.max_context_turns,
                idle_reset_minutes=agent.idle_reset_minutes,
                enable_tools=True,
                enabled=bool(enabled),
            ),
        )
        return RedirectResponse(
            url="/admin/orgs/provisioner?msg=✅ 配置已保存", status_code=303
        )
    except Exception as e:
        logger.error(f"save provisioner failed: {e}")
        return RedirectResponse(
            url=f"/admin/orgs/provisioner?msg=❌ {e}", status_code=303
        )


@router.post("/admin/orgs/provisioner/reset-prompt")
async def admin_provisioner_reset_prompt():
    agent = await _provisioner()
    try:
        # 只回写提示词：配置智能体可能还没填 base_url/model，整表回写会被校验拦下
        await agent_service.update_agent_prompt(agent.id, DEFAULT_PROMPT)
        return RedirectResponse(
            url="/admin/orgs/provisioner?msg=✅ 已恢复默认提示词", status_code=303
        )
    except Exception as e:
        logger.error(f"reset provisioner prompt failed: {e}")
        return RedirectResponse(
            url=f"/admin/orgs/provisioner?msg=❌ {e}", status_code=303
        )


# ── 对话配置台 ──────────────────────────────────────────────

def _trace_html(trace: list) -> str:
    if not trace:
        return (
            '<div class="note note-warn">⚠️ 本轮没有调用任何工具。'
            "把需求说得更具体（组织名 + 客户是谁 + 要收集什么）再试一次。</div>"
        )
    rows = ""
    for i, t in enumerate(trace, 1):
        args_txt = json.dumps(t.get("args") or {}, ensure_ascii=False)[:500]
        result_txt = str(t.get("result") or "")[:600]
        rows += (
            f"<tr><td>{i}</td>"
            f"<td><code>{esc(t.get('tool') or '')}</code></td>"
            f"<td class='mono' style='max-width:300px;font-size:12px'>{esc(args_txt)}</td>"
            f"<td class='mono' style='max-width:340px;font-size:12px'>{esc(result_txt)}</td></tr>"
        )
    return (
        '<h4 style="margin:14px 0 6px">🧰 工具调用轨迹</h4>'
        '<table class="table"><thead><tr><th>#</th><th>工具</th><th>参数</th><th>结果</th></tr>'
        f"</thead><tbody>{rows}</tbody></table>"
    )


@router.get("/admin/orgs/provisioner/chat")
async def admin_provisioner_chat(msg: str = ""):
    agent = await _provisioner()
    return page(
        "AI 配置组织 · 配置台",
        "💬 配置台",
        _console_html(agent),
        msg,
        bare=True,
    )


def _console_html(agent, instruction: str = "", reply: str = "", trace: list = None, error: str = "") -> str:
    result = ""
    if instruction:
        result += (
            '<div class="note note-info" style="white-space:pre-wrap">'
            f"<b>本轮需求：</b>{esc(instruction)}</div>"
        )
    if error:
        result += f'<div class="note note-warn">❌ 执行失败：{esc(error[:600])}</div>'
    elif reply is not None and (reply or trace):
        result += (
            '<h4 style="margin:14px 0 6px">📣 它汇报的结果</h4>'
            '<div class="card" style="white-space:pre-wrap;background:#f8fafc">'
            f"{esc(reply or '（没有文字输出，请看工具执行结果）')}</div>"
            f"{_trace_html(trace or [])}"
        )
    return f"""
    <div class="card">
        <h3>💬 组织配置台</h3>
        <p class="card-sub">
            用中文描述你要建的组织：<b>组织名称</b>、这个组织是干什么的（客户是谁、要收集/回答什么、
            要不要定时任务）。它会直接创建，并把新资源的 id 报给你。
        </p>
        {_status_chips(agent)}
        <div style="margin-top:12px" class="inline-flex">
            <a class="btn" href="/admin/orgs/provisioner" style="background:#f1f2f6;color:#374151;border:1px solid #e5e7eb">🔧 配置 LLM 与提示词</a>
            <a class="btn" href="/admin/orgs/provisioner/chat/clear"
               style="background:#f1f2f6;color:#b91c1c;border:1px solid #e5e7eb"
               onclick="return confirm('清空配置台对话上下文？（已创建的资源不受影响）')">🧹 清空对话</a>
            <a class="btn" href="/admin/orgs" style="background:#f1f2f6;color:#374151;border:1px solid #e5e7eb">← 返回组织管理</a>
        </div>
    </div>

    <form method="POST" action="/admin/orgs/provisioner/chat" class="card form-stack">
        <div class="form-group">
            <label>需求描述</label>
            <textarea name="instruction" rows="6" required
                placeholder="例：给「日照蓝鲸机械」建一个组织：让企微 AI 员工负责回答产品咨询并把有意向的客户记进线索表（客户称呼、联系方式、公司、需求、意向等级、来源会话）；客户问的、答不上来的问题单独记一张表；每天早上 9 点汇总一次前一天的线索。"></textarea>
        </div>
        <div><button class="btn btn-primary">🚀 开始创建</button></div>
    </form>
    {result}
    {_tool_list_html()}
    """


@router.post("/admin/orgs/provisioner/chat")
async def admin_provisioner_chat_run(instruction: str = Form("")):
    agent = await _provisioner()
    text = (instruction or "").strip()
    if not llm_ready(agent):
        return page(
            "AI 配置组织 · 配置台",
            "💬 配置台",
            _console_html(
                agent,
                error="还没有配 LLM（服务商/模型/API Key），先到「配置 LLM 与提示词」页填好再试。",
            ),
            bare=True,
        )
    if not text:
        return RedirectResponse(
            url="/admin/orgs/provisioner/chat?msg=⚠ 请先描述需求", status_code=303
        )

    trace: list = []
    reply, error = None, ""
    try:
        reply = await agent_service.generate_reply(
            agent,
            robot_id=f"test{agent.id}",
            room_name=CONSOLE_ROOM,
            room_type=2,
            user_text=text,
            simulate=True,
            trace=trace,
            reply_policy="none",
        )
    except Exception as e:
        logger.error(f"provision chat failed #{agent.id}: {e}")
        error = str(e)

    return page(
        "AI 配置组织 · 配置台",
        "💬 配置台",
        _console_html(agent, instruction=text, reply=reply, trace=trace, error=error),
        bare=True,
    )


def llm_ready(agent) -> bool:
    return bool(agent.model and agent.api_key and agent.base_url)


@router.get("/admin/orgs/provisioner/chat/clear")
async def admin_provisioner_chat_clear():
    agent = await _provisioner()
    n = await agent_service.clear_test_conversations(agent.id)
    return RedirectResponse(
        url=f"/admin/orgs/provisioner/chat?msg=✅ 已清空 {n} 条配置台会话", status_code=303
    )
