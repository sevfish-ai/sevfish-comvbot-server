"""
智能体（Agent）管理页面（服务端渲染）
"""

import json
import logging
import re
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse

from ..models.agent import AgentCreateRequest
from ..services import (
    agent_service,
    knowledge_service,
    group_service,
    workflow_service,
    robot_service,
    skill_service,
)
from ..layout import page, esc
from .workflows import CMD_OPTIONS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agents"])

# LLM 服务商预设（OpenAI 兼容 /chat/completions）
# 注意：DeepSeek 一项已用真实 key 调 /models 实测；其余地址/模型按 2026-08 官方文档，
#       厂商调整较快，若测试失败请以各平台官方文档为准；base_url 与 model 均可自由修改。
#       标注「境外」的厂商需自备网络代理方可直连。
# 预设模型清单（2026-10 按各服务商在线模型目录校对，DeepSeek 另经其 /models 接口核对）。
# 只是填表便利，任何模型名都能用「✏️ 自定义模型名…」手填。
PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek 深度求索",
        "url": "https://api.deepseek.com",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4.1-flash", "deepseek-flash"],
    },
    "openai": {
        "label": "OpenAI（境外）",
        "url": "https://api.openai.com/v1",
        "models": ["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5", "gpt-5.5-pro", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-4o", "gpt-4o-mini"],
    },
    "qwen": {
        "label": "通义千问 Qwen（阿里云百炼）",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen3.8-max", "qwen3.8-flash", "qwen3.7-max", "qwen3.7-plus", "qwen3.7-flash", "qwen3.6-plus", "qwen3.6-flash", "qwen-plus"],
    },
    "zhipu": {
        "label": "智谱 GLM",
        "url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.2", "glm-5.1", "glm-5", "glm-4.7", "glm-4.7-flash", "glm-4.6"],
    },
    "moonshot": {
        "label": "Kimi 月之暗面",
        "url": "https://api.moonshot.cn/v1",
        "models": ["kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking", "moonshot-v1-128k"],
    },
    "doubao": {
        "label": "豆包（火山方舟）",
        "url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seed-2.1-turbo", "doubao-seed-2.0-mini", "doubao-seed-2.0-lite", "doubao-seed-evolving"],
    },
    "hunyuan": {
        "label": "腾讯混元",
        "url": "https://api.hunyuan.cloud.tencent.com/v1",
        "models": ["hunyuan-turbos-latest", "hunyuan-t1-latest"],
    },
    "minimax": {
        "label": "MiniMax",
        "url": "https://api.minimaxi.com/v1",
        "models": ["MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5", "MiniMax-M2.5-highspeed", "MiniMax-M2"],
    },
    "gemini": {
        "label": "Google Gemini（境外）",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-4-argon", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"],
    },
    "openrouter": {
        "label": "OpenRouter（境外聚合，可调 Claude 等）",
        "url": "https://openrouter.ai/api/v1",
        "models": [],
    },
    "custom": {"label": "自定义（OpenAI 兼容）", "url": "", "models": []},
}


def _provider_select(selected: str = "deepseek") -> str:
    opts = "".join(
        f'<option value="{p}"{" selected" if p == selected else ""}>{info["label"]}</option>'
        for p, info in PROVIDERS.items()
    )
    # 库里存的服务商标识不在预设表里时（自建/改名过）保留原值，别让保存时被悄悄改成预设第一项
    if selected and selected not in PROVIDERS:
        opts = f'<option value="{esc(selected)}" selected>{esc(selected)}</option>' + opts
    return (
        f'<select name="provider" class="f-provider" onchange="onProviderChange(this)">{opts}</select>'
    )


def _switch_html(name: str, checked: bool, on_text: str, off_text: str) -> str:
    """渲染高对比度开关（toggle switch），替代易遗漏的复选框。状态颜色 + 文字双提示。"""
    chk = " checked" if checked else ""
    init_text = on_text if checked else off_text
    return (
        f'<label class="agent-switch">'
        f'<input type="checkbox" name="{name}"{chk} data-on="{on_text}" data-off="{off_text}">'
        f'<span class="slider"></span></label>'
        f'<span class="agent-switch-text">{init_text}</span>'
    )


# 开关的样式与「勾选后文字跟着变」的脚本：智能体表单与「AI 配置组织」配置页共用
SWITCH_CSS = """<style>
    .agent-switch{position:relative;display:inline-block;width:46px;height:24px;vertical-align:middle;flex:none}
    .agent-switch input{opacity:0;width:0;height:0;margin:0;position:absolute}
    .agent-switch .slider{position:absolute;cursor:pointer;inset:0;background:#cbd5e1;border-radius:24px;transition:.2s}
    .agent-switch .slider:before{content:"";position:absolute;height:18px;width:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s;box-shadow:0 1px 2px rgba(0,0,0,.2)}
    .agent-switch input:checked + .slider{background:#22c55e}
    .agent-switch input:checked + .slider:before{transform:translateX(22px)}
    .agent-switch-text{font-size:13px;color:#475569;min-width:36px;display:inline-block;text-align:left}
</style>"""

SWITCH_JS = """<script>
    (function(){
        document.querySelectorAll('.agent-switch input').forEach(function(i){
            function apply() {
                var t = i.parentElement.parentElement.querySelector('.agent-switch-text');
                if (t) t.textContent = i.checked ? i.dataset.on : i.dataset.off;
            }
            i.addEventListener('change', apply);
            apply();
        });
    })();
</script>"""


def provider_model_fields(provider: str, base_url: str, model: str, *, required: bool = True) -> str:
    """LLM 服务商 / base_url / 模型 三个表单项（模型为下拉 + 「自定义模型名」入口）。

    服务商切换时的联动由 provider_model_js() 负责。
    """
    preset_models = PROVIDERS.get(provider, {}).get("models", [])
    model_options = "".join(
        f'<option value="{esc(m)}"{(" selected" if m == model else "")}>{esc(m)}</option>'
        for m in preset_models
    )
    star = " *" if required else ""
    req = " required" if required else ""
    return f"""
        <div class="form-group"><label>LLM 服务商</label>{_provider_select(provider)}</div>
        <div class="form-group"><label>API 地址 base_url{star}</label>
            <input type="text" name="base_url" class="f-base_url" value="{esc(base_url)}"
                   placeholder="https://api.deepseek.com"{req}></div>
        <div class="form-group"><label>模型 model{star}</label>
            <select name="model" class="f-model w-100" data-model="{esc(model)}"
                    onchange="onModelChange(this)">
                {model_options}
                <option value="__custom__">✏️ 自定义模型名…</option>
            </select>
            <input type="text" class="f-model-custom" placeholder="输入自定义模型名"
                   style="display:none;margin-top:6px"></div>"""


def provider_model_js() -> str:
    """服务商 → base_url/模型下拉的联动脚本（每个含 .f-provider 的表单自动初始化）。"""
    return (
        "<script>\nconst PROVIDERS = "
        + json.dumps(PROVIDERS, ensure_ascii=False)
        + """;
    // 切换服务商：重建模型下拉选项并自动回填 base_url（keepUrl=true 时保留已填地址，供表单初始化）
    function onProviderChange(sel, keepUrl){
        var cfg = PROVIDERS[sel.value] || PROVIDERS.custom;
        var form = sel.closest('form');
        var url = form.querySelector('.f-base_url');
        if (url && !keepUrl) { url.value = cfg.url; }
        var msel = form.querySelector('.f-model');
        var cur = msel.value;
        msel.innerHTML = '';
        cfg.models.forEach(function(x){
            var o = document.createElement('option'); o.value = x; o.textContent = x; msel.appendChild(o);
        });
        var cus = document.createElement('option');
        cus.value = '__custom__'; cus.textContent = '✏️ 自定义模型名…';
        msel.appendChild(cus);
        if (cfg.models.indexOf(cur) >= 0) { msel.value = cur; }
        else { msel.value = cfg.models.length ? cfg.models[0] : '__custom__'; }
        onModelChange(msel);
    }
    // 模型下拉：选择「自定义」时显示输入框，并移交 name 使自定义值参与提交
    function onModelChange(sel){
        var form = sel.closest('form');
        var custom = form.querySelector('.f-model-custom');
        if (sel.value === '__custom__') {
            custom.style.display = 'block';
            sel.removeAttribute('name');
            custom.setAttribute('name', 'model');
            custom.focus();
        } else {
            custom.style.display = 'none';
            custom.removeAttribute('name');
            sel.setAttribute('name', 'model');
        }
    }
    // 页面初始化：按 data-model 回显（预设中没有的模型切到自定义并回填）
    function initForm(form){
        var psel = form.querySelector('.f-provider');
        if (!psel) { return; }
        onProviderChange(psel, true);
        var sel = form.querySelector('.f-model');
        var custom = form.querySelector('.f-model-custom');
        var cur = sel.getAttribute('data-model') || '';
        if (cur) {
            var has = Array.prototype.some.call(sel.options, function(o){ return o.value === cur; });
            if (has) { sel.value = cur; }
            else { sel.value = '__custom__'; custom.value = cur; }
        }
        onModelChange(sel);
    }
    document.addEventListener('DOMContentLoaded', function(){
        document.querySelectorAll('form').forEach(function(f){
            if (f.querySelector('.f-provider')) { initForm(f); }
        });
    });
    </script>"""
    )


# 系统提示词的区块标题预设（下拉里可直接选，其余走「自定义标题」）
PROMPT_TITLE_PRESETS = (
    "角色", "职责", "背景", "目标", "行为规范", "处理流程", "回复要求",
    "对话示例", "知识来源", "安全边界", "设备指令", "工作流与任务", "数据与联网",
)
_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$")


def split_prompt(text: str) -> list:
    """把 system_prompt 拆成 [(标题, 正文)]：`# 标题` 行分段，之前的内容算无标题区块。

    历史提示词（纯段落、自己写的 `# 小标题`）都能拆回来且内容不丢，只是区块间空白被规整。
    """
    sections = []
    title, buf = "", []
    for line in (text or "").splitlines():
        m = _HEADING_RE.match(line)
        if m:
            sections.append((title, "\n".join(buf).strip()))
            title, buf = m.group(1), []
        else:
            buf.append(line)
    sections.append((title, "\n".join(buf).strip()))
    return [(t, b) for t, b in sections if t or b]


def join_prompt(titles: List[str], bodies: List[str]) -> str:
    """把表单区块拼成最终给智能体的多行文本；正文为空（或没有正文）的区块不输出。"""
    parts = []
    for title, body in zip(titles, bodies):
        body = (body or "").strip()
        if not body:
            continue
        title = (title or "").strip().lstrip("#").strip()
        # 正文自己就以 `# 标题` 开头（整段粘贴模板）时原样输出，不再重复加标题
        if not title or _HEADING_RE.match(body.split("\n", 1)[0]):
            parts.append(body)
        else:
            parts.append(f"# {title}\n{body}")
    return "\n\n".join(parts)


def _prompt_row(title: str, body: str) -> str:
    """一个提示词区块：标题下拉（角色/设备指令/无标题/自定义）+ 正文多行文本。"""
    title = title or ""
    custom = bool(title) and title not in PROMPT_TITLE_PRESETS
    presets = "".join(
        f'<option value="{esc(t)}"{" selected" if t == title else ""}>{esc(t)}</option>'
        for t in PROMPT_TITLE_PRESETS
    )
    # 自定义标题时 name 落在输入框上，其余（含「无标题」的空值）落在下拉上：
    # 每个区块恒定提交一个 sp_title，与 sp_body 按顺序一一对应
    sel_name = "" if custom else ' name="sp_title"'
    inp_name = ' name="sp_title"' if custom else ""
    return f"""
    <div class="prompt-row" style="border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px;display:flex;flex-direction:column;gap:8px;background:var(--surface)">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <select class="f-ptitle"{sel_name} onchange="onPromptTitleChange(this)" style="width:auto">
                <option value=""{" selected" if not title else ""}>（无标题，直接输出正文）</option>
                {presets}
                <option value="__custom__"{" selected" if custom else ""}>✏️ 自定义标题…</option>
            </select>
            <input type="text" class="f-ptitle-custom" placeholder="自定义标题"
                   value="{esc(title) if custom else ''}"{inp_name}
                   style="{'display:none;' if not custom else ''}flex:1;min-width:160px">
            <span style="flex:1"></span>
            <button type="button" class="btn btn-danger btn-sm" onclick="delPromptRow(this)">删除此区块</button>
        </div>
        <textarea name="sp_body" rows="5" placeholder="这一区块的提示词内容…">{esc(body)}</textarea>
    </div>"""


def _prompt_form_html(system_prompt: str) -> str:
    """系统提示词区块表单（含一份空区块模板，供 JS 克隆新增）。"""
    rows = "".join(_prompt_row(t, b) for t, b in split_prompt(system_prompt))
    rows += _prompt_row("角色", "")  # 末尾恒留一个空区块，省得还要先点「添加」
    return f"""
            <div class="form-group full">
                <label>系统提示词（按区块填写，保存时自动拼成给智能体的多行文本）</label>
                <div class="prompt-rows" style="display:flex;flex-direction:column;gap:10px">{rows}</div>
                <div>
                    <button type="button" class="btn btn-primary btn-sm" onclick="addPromptRow(this)">＋ 添加区块</button>
                </div>
                <div class="form-hint">每块输出为「# 标题」+ 换行 + 正文，块与块之间空一行；正文留空则不输出。选「（无标题）」、或正文自己就以 <code># 标题</code> 开头（整段粘贴模板）时，只输出正文。</div>
                <template class="t-prompt-row">{_prompt_row("角色", "")}</template>
            </div>"""


def _agent_form(
    action: str,
    v: dict = None,
    is_edit: bool = False,
    kbs: list = None,
    selected_kb_ids: set = None,
    workflows: list = None,
    selected_wf_ids: set = None,
    selected_cmd_ids: set = None,
    skills: list = None,
    selected_skill_ids: set = None,
) -> str:
    """渲染智能体表单（新建/编辑共用）。v 为预填值 dict；kbs=知识库列表；selected_kb_ids=已选知识库 id 集合。
    workflows=可选工作流列表；selected_wf_ids=已选工作流 id 集合；selected_cmd_ids=已选设备指令类型集合。"""
    v = v or {}
    name = esc(v.get("name", ""))
    description = esc(v.get("description", ""))
    provider = v.get("provider", "deepseek")
    base_url = esc(v.get("base_url", ""))
    model = esc(v.get("model", ""))
    system_prompt = v.get("system_prompt", "") or ""
    temperature = v.get("temperature", 0.7)
    max_tokens = v.get("max_tokens", 8192)
    max_context_turns = v.get("max_context_turns", 20)
    idle_reset_minutes = v.get("idle_reset_minutes", 30)
    image_strategy = int(v.get("image_strategy", 0))
    enable_http = bool(v.get("enable_http", False))
    allow_table_create = bool(v.get("allow_table_create", False))
    allow_task_create = bool(v.get("allow_task_create", False))
    enabled = bool(v.get("enabled", True))

    img_strategy_html = f"""
    <select name="image_strategy" class="f-select w-100">
        <option value="0"{(" selected" if image_strategy == 0 else "")}>传递路径（图片只传服务器路径）</option>
        <option value="1"{(" selected" if image_strategy == 1 else "")}>传递图片（Base64，LLM 视觉识别）</option>
        <option value="2"{(" selected" if image_strategy == 2 else "")}>OCR 识别图片文字（PaddleOCR）</option>
    </select>"""

    key_html = (
        f'<input type="password" name="api_key" placeholder="sk-..." required>'
        if not is_edit
        else f'<input type="password" name="api_key" placeholder="留空表示保持不变">'
    )

    # 知识库引用（多选）
    sel_ids = selected_kb_ids or set()
    if kbs:
        kb_options = "".join(
            f'<label class="chip-check">'
            f'<input type="checkbox" name="knowledge_ids" value="{kb.id}"'
            f'{(" checked" if kb.id in sel_ids else "")}> {esc(kb.name)}</label>'
            for kb in kbs
        )
        kb_html = f'<div class="inline-flex" style="gap:8px">{kb_options}</div>'
    else:
        kb_html = (
            '<span class="form-tip">暂无知识库，请先到「知识库」页创建</span>'
        )

    # 可调用工作流（多选，仅同组织）
    sel_wf = selected_wf_ids or set()
    if workflows:
        wf_options = "".join(
            f'<label class="chip-check"{" style=border-color:#f59e0b;background:#fef3c7" if getattr(w, "is_sensitive", False) else ""}>'
            f'<input type="checkbox" name="workflow_ids" value="{w.id}"'
            f'{(" checked" if w.id in sel_wf else "")}> #{w.id} {esc(w.name)}'
            f'{"（停用）" if not w.enabled else ""}'
            f'{" ⚠️敏感" if getattr(w, "is_sensitive", False) else ""}</label>'
            for w in workflows
        )
        wf_html = (
            f'<div class="inline-flex" style="gap:8px;flex-wrap:wrap">{wf_options}</div>'
            f'<div class="form-tip" style="color:#b45309">⚠️ 敏感工作流（涉及删除/导出/外部副作用）授权给对话智能体时请谨慎评估</div>'
        )
    else:
        wf_html = '<span class="form-tip">当前组织暂无工作流，请先到「工作流」页创建</span>'

    # 可调用设备指令（多选）
    sel_cmd = selected_cmd_ids or set()
    cmd_options = "".join(
        f'<label class="chip-check">'
        f'<input type="checkbox" name="cmd_types" value="{c}"'
        f'{(" checked" if int(c) in sel_cmd else "")}> {c} {esc(label)}</label>'
        for c, label in CMD_OPTIONS
    )
    cmd_html = f'<div class="inline-flex" style="gap:8px;flex-wrap:wrap">{cmd_options}</div>'

    # 启用技能（多选，智能体侧白名单：勾选后才触发）
    sel_skill = selected_skill_ids or set()
    if skills:
        skill_options = "".join(
            f'<label class="chip-check">'
            f'<input type="checkbox" name="skill_ids" value="{s["id"]}"'
            f'{(" checked" if s["id"] in sel_skill else "")}> {esc(s["name"])}'
            f'{"（停用）" if not s["enabled"] else ""}</label>'
            for s in skills
        )
        skill_html = f'<div class="inline-flex" style="gap:8px;flex-wrap:wrap">{skill_options}</div>'
    else:
        skill_html = '<span class="form-tip">暂无技能，请先到「技能」页创建</span>'

    # 所属组织：创建时按进入的组织自动归属，表单不提供修改入口

    # 模型下拉：列出当前服务商预设的全部模型，另附「自定义模型名」入口
    model_html = provider_model_fields(provider, base_url, model)

    return f"""
    {SWITCH_CSS}
    <form method="POST" action="{action}" class="form-stack" data-agent-form>
        <div class="form-grid">
            <div class="form-group"><label>智能体名称 *</label><input type="text" name="name" value="{name}" required></div>
            <div class="form-group"><label>描述</label><input type="text" name="description" value="{description}" placeholder="用途说明"></div>
            {model_html}
            <div class="form-group"><label>API Key *</label>{key_html}</div>
            <div class="form-group"><label>温度 temperature（0~2，越高越随机）</label><input type="number" name="temperature" value="{temperature}" step="0.1" min="0" max="2"></div>
            <div class="form-group"><label>最大回复 max_tokens</label><input type="number" name="max_tokens" value="{max_tokens}" min="1"><div class="form-hint">思考 token 也算在这个预算里，太小会把预算耗在思考上导致正文为空（默认 8192，别低于 4096）</div></div>
            <div class="form-group"><label>上下文保留轮数（防对话过长）</label><input type="number" name="max_context_turns" value="{max_context_turns}" min="1" max="200"></div>
            <div class="form-group"><label>空闲 N 分钟重置会话（0=不重置）</label><input type="number" name="idle_reset_minutes" value="{idle_reset_minutes}" min="0" max="1440"></div>
            <div class="form-group"><label>图片消息处理</label>{img_strategy_html}</div>
            <div class="form-group full">
                <label>知识库引用（回复时检索这些知识库作为参考资料，可多选）</label>
                {kb_html}
            </div>
            <div class="form-group full">
                <label>可调用工作流（勾选后，智能体在对话中可按需运行这些工作流，仅限同组织）</label>
                {wf_html}
            </div>
            <div class="form-group full">
                <label>可调用设备指令（勾选后，智能体在对话中可按需向当前设备下发这些指令）</label>
                {cmd_html}
            </div>
            <div class="form-group full">
                <label>启用技能（勾选后，命中触发条件的技能指令会注入该智能体的对话；工作流 LLM 节点挂载不受此勾选约束）</label>
                {skill_html}
            </div>
            <div class="form-group form-switch-row">
                <label class="mb-0">启用（停用则不回复）</label>
                {_switch_html("enabled", enabled, "启用", "停用")}
            </div>
            <div class="form-group form-switch-row">
                <label class="mb-0" title="开启后 LLM 可调用 http_get / http_post 访问链接">启用联网工具</label>
                {_switch_html("enable_http", enable_http, "开启", "关闭")}
            </div>
            <div class="form-group form-switch-row">
                <label class="mb-0" title="开启后 LLM 可通过 table_create 创建新表格（创建后自动授权本智能体读写）。表格查询/写入能力由「表格管理 → 授权给智能体」控制">允许创建表格</label>
                {_switch_html("allow_table_create", allow_table_create, "允许", "禁止")}
            </div>
            <div class="form-group form-switch-row">
                <label class="mb-0" title="开启后 LLM 可通过 task_create 工具创建定时/条件任务">允许创建任务</label>
                {_switch_html("allow_task_create", allow_task_create, "允许", "禁止")}
            </div>
            {_prompt_form_html(system_prompt)}
        </div>
        <div>
            <button type="submit" class="btn btn-primary">保存</button>
        </div>
    </form>
    {SWITCH_JS}
    <script>
        // 提示词区块：切换「自定义标题」时把 name 在 select / input 之间移交，
        // 保证每个区块恒定提交一个 sp_title（与 sp_body 顺序对齐）
        function onPromptTitleChange(sel){{
            var row = sel.closest('.prompt-row');
            var inp = row.querySelector('.f-ptitle-custom');
            if (sel.value === '__custom__') {{
                sel.removeAttribute('name');
                inp.setAttribute('name', 'sp_title');
                inp.style.display = '';
                inp.focus();
            }} else {{
                inp.removeAttribute('name');
                inp.style.display = 'none';
                sel.setAttribute('name', 'sp_title');
            }}
        }}
        function delPromptRow(btn){{
            btn.closest('.prompt-row').remove();
        }}
        function addPromptRow(btn){{
            var form = btn.closest('form');
            var tpl = form.querySelector('template.t-prompt-row');
            form.querySelector('.prompt-rows').appendChild(tpl.content.cloneNode(true));
        }}
    </script>"""


@router.get("/admin/agents")
async def admin_agents(msg: str = "", org_id: int = 0):
    """智能体管理页（支持 ?org_id= 按组织过滤）"""
    all_agents = await agent_service.list_agents()
    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if not grp:
            return RedirectResponse(
                url="/admin/orgs?msg=❌ 组织不存在", status_code=303
            )
        cur_org_name = grp["name"]
        member_ids = set(grp["members"].get("agent", []))
        all_agents = [a for a in all_agents if str(a.id) in member_ids]

    agents = all_agents
    kbs = await knowledge_service.list_kbs(enabled_only=True)
    skills = await skill_service.list_skills()
    agent_groups = await group_service.list_org_groups()
    agent_group_name = {g["id"]: g["name"] for g in agent_groups}

    # 可调用工作流：仅同组织（org_id=0 时展示全部）
    all_wfs = await workflow_service.list_workflows()
    if org_id:
        org_wf_ids = set(await group_service.get_group_org_members(org_id, "workflow"))
        org_wf_ids = {int(x) for x in org_wf_ids}
        workflows = [w for w in all_wfs if w.id in org_wf_ids]
    else:
        workflows = all_wfs


    rows_parts = []
    for a in agents:
        sel_kbs = set(await knowledge_service.get_agent_kbs(a.id))
        sel_groups = set(await group_service.get_member_org_groups("agent", a.id))
        sel_wfs = set(await agent_service.get_agent_workflows(a.id))
        sel_cmds = set(await agent_service.get_agent_commands(a.id))
        sel_skills = set(await skill_service.get_agent_skills(a.id))
        provider_label = PROVIDERS.get(a.provider, {}).get("label", a.provider)
        key_preview = f"••••{a.api_key[-4:]}" if a.api_key else "-"
        status_badge = (
            '<span class="badge badge-online">启用</span>'
            if a.enabled
            else '<span class="badge badge-offline">停用</span>'
        )
        img_strategy_label = {0: "路径", 1: "Base64 视觉", 2: "OCR 文字"}.get(
            int(a.image_strategy or 0), "路径"
        )
        rows_parts.append(
            f"<tr>"
            f"<td><code>#{a.id}</code></td>"
            f"<td><span class='cell-main'>{esc(a.name)}</span><span class='cell-sub'>{esc(a.description)}</span></td>"
            f"<td>{esc(provider_label)}<br><code>{esc(a.model)}</code></td>"
            f"<td><code>{key_preview}</code></td>"
            f"<td>temp={a.temperature}<br>max_tokens={a.max_tokens}</td>"
            f"<td>{a.max_context_turns} 轮<br>空闲 {a.idle_reset_minutes} 分钟重置</td>"
            f"<td>{img_strategy_label}</td>"
            f"<td>{'✅ 联网' if a.enable_http else '—'}<br>{'✅ 创建任务' if getattr(a, 'allow_task_create', False) else '—'}</td>"
            f"<td>{len(sel_kbs)} 个</td>"
            f"<td>{len(sel_wfs)} 工作流<br>{len(sel_cmds)} 指令</td>"
            f"<td>{'、'.join(esc(agent_group_name[gid]) for gid in sorted(sel_groups) if gid in agent_group_name) or '—'}</td>"
            f"<td>{status_badge}</td>"
            f"<td>{a.robot_count} 台</td>"
            f"<td>"
            f"<form method='POST' action='/admin/agent/{a.id}/test{f'?org_id={org_id}' if org_id else ''}' class='inline-form'>"
            f"<button class='btn btn-success btn-sm'>测试</button></form> "
            f"<a href='/admin/agent/{a.id}/test_run{f'?org_id={org_id}' if org_id else ''}' class='btn btn-warning btn-sm'>测试运行</a> "
            f"<button class='btn btn-primary btn-sm' onclick=\"toggleEdit({a.id})\">编辑</button> "
            f"<a href='/admin/agent/{a.id}/delete{f'?org_id={org_id}' if org_id else ''}' class='btn btn-danger btn-sm' "
            f"onclick=\"return confirm('删除智能体「{esc(a.name)}」？绑定的 {a.robot_count} 台设备将解绑，相关会话记录一并删除！')\">删除</a>"
            f"</td>"
            f"</tr>"
            f"<tr id='edit-{a.id}' style='display:none;background:#f8fafc'>"
            f"<td colspan='14'>"
            f"{_agent_form(f'/admin/agent/{a.id}/update?org_id={org_id}' if org_id else f'/admin/agent/{a.id}/update', a.model_dump(), is_edit=True, kbs=kbs, selected_kb_ids=sel_kbs, workflows=workflows, selected_wf_ids=sel_wfs, selected_cmd_ids=sel_cmds, skills=skills, selected_skill_ids=sel_skills)}"
            f"</td></tr>"
        )
    rows_html = "\n".join(rows_parts) if rows_parts else '<tr><td colspan="14" class="empty">暂无智能体，先在上方创建</td></tr>'

    table_html = f"""
    <div class="table-wrap">
    <table>
        <thead><tr><th>ID</th><th>名称</th><th>服务商 / 模型</th><th>API Key</th><th>采样参数</th><th>会话管理</th><th>图片策略</th><th>联网 / 建任务</th><th>知识库</th><th>授权</th><th>组织</th><th>状态</th><th>绑定设备</th><th>操作</th></tr></thead>
        <tbody>{rows_html}</tbody>
    </table>
    </div>"""

    note_html = """
    <div class="note note-info">
        <b>工作原理：</b>设备收到新消息后，服务器调用该设备绑定的智能体 → 智能体通过 <code>/chat/completions</code>（OpenAI 兼容协议）请求 LLM 生成回复 → 服务器下发 203 指令由设备发送到企微。<br>
        <b>对话管理：</b>按「设备 + 房间」独立保存对话上下文（自动落库）。上下文最多保留 <code>max_context_turns</code> 轮，超出后丢弃最旧；空闲超过 <code>idle_reset_minutes</code> 分钟自动开启新会话；更换绑定智能体也会重置会话。<br>
        <b>厂商预设（DeepSeek 已用 /models 接口实测，其余按官方文档，2026-08）：</b>DeepSeek 仅提供 <code>deepseek-v4-pro</code> 与 <code>deepseek-flash</code>（<code>deepseek-v4-flash</code> 为旧别名仍可用；旧 deepseek-chat/reasoner 已退役）；OpenAI 最新为 <code>gpt-5.5</code>；通义千问最新 <code>qwen3.8-max</code>；智谱旗舰 <code>glm-5.2</code>（1M 上下文）；Kimi 新增开源旗舰 <code>kimi-k3</code>（2.8 万亿参数）。<br>
        <b>千问地址注意：</b><code>qwen3.8-max / qwen3.7-plus / qwen3.7-flash</code> 需使用百炼新版 maas 地址 <code>https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1</code>（WorkspaceId 在百炼控制台查询），旧通用地址 <code>dashscope.aliyuncs.com/compatible-mode/v1</code> 只支持到 <code>qwen3.6</code> 及以下，请在 base_url 中填写。<br>
        <b>豆包注意：</b>火山方舟需先在控制台「开通」对应模型服务；个别模型可能要求用推理接入点 ID（<code>ep-...</code>）作为 model，可在「自定义模型名」中填写。<br>
        <b>境外模型：</b>OpenAI / Gemini 标注「境外」，本机需能访问外网（代理）方可调用；Claude 原生走 Anthropic 协议，本系统不支持，可通过 <code>OpenRouter</code>（OpenAI 兼容聚合）中转调用，模型名按 OpenRouter 官方格式（如 <code>anthropic/...</code>）填写。<br>
        <b>图片消息处理：</b>收到图片消息时按所选策略处理 —— <code>传递路径</code>：只把服务器上的图片路径文本发给 LLM（兼容所有模型）；<code>传递图片</code>：图片压缩到最长边 1024 后转 Base64 发给 LLM 做视觉识别（需模型支持图片，如 OpenAI / Gemini / qwen3.7-plus / Kimi k3 / MiniMax-M3；DeepSeek 等不支持时自动降级为传路径文本）；<code>OCR 识别图片文字</code>：用本地 PaddleOCR 提取图片中的文字后作为文本发给 LLM（不依赖模型视觉能力，需执行 <code>pip install paddleocr onnxruntime</code>）。多张图片时最多取最近 2 张。<br>
        <b>联网工具（Function Calling）：</b>开启「启用联网工具」开关后，LLM 可根据对话内容自行决定调用 <code>http_get</code>（GET 访问链接）或 <code>http_post</code>（POST 提交 JSON 数据）两个工具，服务器执行真实 HTTP 请求并把结果回传给 LLM 生成最终回复。例如在系统提示词中写明「查询物流时访问 https://api.xxx.com/track 并 POST {"order_no": "..."}」，即可让智能体按需联网查询。注意：1) 需要 LLM 支持 Function Calling（主流模型均支持）；2) 仅允许 <code>http/https</code> 协议，结果超过 6000 字符自动截断；3) 最多连续调用 5 轮，防止死循环。<br>
        <b>表格工具（Function Calling）：</b>与「启用联网工具」无关，表格能力由两部分控制：① 查询/写入已授权表格（<code>table_query</code> / <code>table_insert</code> / <code>table_update</code>）需在「表格管理 → 授权给智能体」中授权（查询需读权限，插入/更新需读写权限）；② 创建新表格（<code>table_create</code>，字段可自定义）需开启「允许创建表格」开关，创建成功后自动授权给当前智能体读写。例如系统提示词写明「用户报修时：若没有该客户表格，先用 table_create 建表，再用 table_insert 记录」，即可让智能体按需建表并保存数据。<br>
        <b>工作流与设备指令（勾选授权）：</b>在表单中勾选「可调用工作流」与「可调用设备指令」后，智能体在对话中可按需运行这些工作流（仅限同组织）、或向当前会话绑定的设备下发指令（发送消息/创建群/获取群信息等）。<b>设备聊天里的回复就是靠设备指令 <code>203 发送消息</code> 发出的</b>：服务器把当前会话名、会话类型、发言者直接写进提示词，智能体自行决定回不回、回什么，再调 203 发到该会话——服务器不再代发它的文本输出。因此要让智能体在设备会话里回复，<u>必须勾选 <code>203 发送消息</code></u>（群聊真 @ 还需它自己带 at/atList）。勾选前请先在工作流页创建对应工作流；设备指令下发需设备在线，且同一智能体可对多台设备使用。工作流内部的 LLM 节点不会再触发工作流工具，防止递归死循环。<br>
        <b>创建任务（勾选授权）：</b>勾选「允许创建任务」后，智能体在对话中可按需调用 <code>task_create</code> 工具创建定时/条件触发任务（任务管理页可见），例如用户说「每天早上 9 点提醒我」时自动创建定时任务。
        <b>注意：</b>厂商模型调整较快，测试失败请以各平台官方文档为准，base_url 与模型名均可手动修改。
    </div>"""

    content = f"""
    <div class="card">
        <h3>🤖 新建智能体</h3>
        <p class="card-sub">对接 DeepSeek / OpenAI / 通义千问 / 智谱 / Kimi 等 LLM（OpenAI 兼容协议），同一智能体可绑定多台设备。</p>
        {note_html}
        {_agent_form(f"/admin/agent/create?org_id={org_id}" if org_id else "/admin/agent/create", kbs=kbs, workflows=workflows, skills=skills)}
    </div>
    <div class="card">
        <h3>📋 智能体列表</h3>
        {table_html}
    </div>
    {provider_model_js()}
    <script>
    function toggleEdit(id){{
        var el = document.getElementById('edit-' + id);
        if (el) el.style.display = (el.style.display === 'none') ? '' : 'none';
    }}
    </script>"""

    return page(
        "智能体", "🤖 智能体管理", content, msg, active="agents",
        org_id=org_id, org_name=cur_org_name, org_active="agents",
    )


# ── POST / GET 操作 ──────────────────────────────────


def _form_to_request(
    name, description, provider, base_url, model, api_key,
    system_prompt, temperature, max_tokens, max_context_turns,
    idle_reset_minutes, enabled, image_strategy=0, enable_tools=True,
    enable_http=False, allow_table_create=False,
    allow_task_create=False,
) -> AgentCreateRequest:
    return AgentCreateRequest(
        name=name,
        description=description,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        max_context_turns=max_context_turns,
        idle_reset_minutes=idle_reset_minutes,
        image_strategy=image_strategy,
        enable_tools=enable_tools,
        enable_http=enable_http,
        allow_table_create=allow_table_create,
        allow_task_create=allow_task_create,
        enabled=enabled,
    )


@router.post("/admin/agent/create")
async def admin_agent_create(
    org_id: int = Query(0),
    name: str = Form(...),
    description: str = Form(""),
    provider: str = Form("custom"),
    base_url: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
    system_prompt: str = Form(""),
    sp_title: List[str] = Form([]),
    sp_body: List[str] = Form([]),
    temperature: float = Form(0.7),
    max_tokens: int = Form(8192),
    max_context_turns: int = Form(20),
    idle_reset_minutes: int = Form(30),
    image_strategy: int = Form(0),
    enable_tools: bool = Form(True),
    enable_http: bool = Form(False),
    allow_table_create: bool = Form(False),
    allow_task_create: bool = Form(False),
    enabled: bool = Form(False),
    knowledge_ids: List[int] = Form([]),
    workflow_ids: List[int] = Form([]),
    cmd_types: List[int] = Form([]),
    skill_ids: List[int] = Form([]),
):
    try:
        if sp_body:  # 区块表单：拼成最终的多行提示词（区块全空 = 清空提示词）
            system_prompt = join_prompt(sp_title, sp_body)
        req = _form_to_request(
            name, description, provider, base_url, model, api_key,
            system_prompt, temperature, max_tokens, max_context_turns,
            idle_reset_minutes, enabled, image_strategy, enable_tools,
            enable_http, allow_table_create, allow_task_create,
        )
        new_id = await agent_service.create_agent(req)
        await knowledge_service.set_agent_kbs(new_id, knowledge_ids)
        # 仅同组织的工作流才允许授权（org_id=0 视为全部）
        if org_id:
            org_wf_ids = set(await group_service.get_group_org_members(org_id, "workflow"))
            org_wf_ids = {int(x) for x in org_wf_ids}
            workflow_ids = [w for w in workflow_ids if int(w) in org_wf_ids]
        await agent_service.set_agent_workflows(new_id, workflow_ids)
        await agent_service.set_agent_commands(new_id, cmd_types)
        await skill_service.set_agent_skills(new_id, skill_ids)
        # 在组织上下文中创建则自动归属该组织（创建后不可改）
        if org_id:
            await group_service.set_member_org_groups("agent", new_id, [org_id])
        base = f"/admin/agents?org_id={org_id}" if org_id else "/admin/agents"
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=✅ 智能体已创建", status_code=303)
    except Exception as e:
        logger.error(f"create agent failed: {e}")
        base = f"/admin/agents?org_id={org_id}" if org_id else "/admin/agents"
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ {esc(str(e))}", status_code=303)


@router.post("/admin/agent/{agent_id}/update")
async def admin_agent_update(
    agent_id: int,
    org_id: int = Query(0),
    name: str = Form(...),
    description: str = Form(""),
    provider: str = Form("custom"),
    base_url: str = Form(""),
    model: str = Form(""),
    api_key: str = Form(""),
    system_prompt: str = Form(""),
    sp_title: List[str] = Form([]),
    sp_body: List[str] = Form([]),
    temperature: float = Form(0.7),
    max_tokens: int = Form(8192),
    max_context_turns: int = Form(20),
    idle_reset_minutes: int = Form(30),
    image_strategy: int = Form(0),
    enable_tools: bool = Form(True),
    enable_http: bool = Form(False),
    allow_table_create: bool = Form(False),
    allow_task_create: bool = Form(False),
    enabled: bool = Form(False),
    knowledge_ids: List[int] = Form([]),
    workflow_ids: List[int] = Form([]),
    cmd_types: List[int] = Form([]),
    skill_ids: List[int] = Form([]),
):
    try:
        if sp_body:  # 区块表单：拼成最终的多行提示词（区块全空 = 清空提示词）
            system_prompt = join_prompt(sp_title, sp_body)
        req = _form_to_request(
            name, description, provider, base_url, model, api_key,
            system_prompt, temperature, max_tokens, max_context_turns,
            idle_reset_minutes, enabled, image_strategy, enable_tools,
            enable_http, allow_table_create, allow_task_create,
        )
        success = await agent_service.update_agent(agent_id, req)
        base = f"/admin/agents?org_id={org_id}" if org_id else "/admin/agents"
        if not success:
            return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ 智能体不存在", status_code=303)
        await knowledge_service.set_agent_kbs(agent_id, knowledge_ids)
        # 仅同组织的工作流才允许授权（org_id=0 视为全部）
        if org_id:
            org_wf_ids = set(await group_service.get_group_org_members(org_id, "workflow"))
            org_wf_ids = {int(x) for x in org_wf_ids}
            workflow_ids = [w for w in workflow_ids if int(w) in org_wf_ids]
        await agent_service.set_agent_workflows(agent_id, workflow_ids)
        await agent_service.set_agent_commands(agent_id, cmd_types)
        await skill_service.set_agent_skills(agent_id, skill_ids)
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=✅ 智能体已更新", status_code=303)
    except Exception as e:
        logger.error(f"update agent failed #{agent_id}: {e}")
        base = f"/admin/agents?org_id={org_id}" if org_id else "/admin/agents"
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ {esc(str(e))}", status_code=303)


@router.get("/admin/agent/{agent_id}/delete")
async def admin_agent_delete(agent_id: int, org_id: int = Query(0)):
    base = f"/admin/agents?org_id={org_id}" if org_id else "/admin/agents"
    try:
        success = await agent_service.delete_agent(agent_id)
        if not success:
            return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ 智能体不存在", status_code=303)
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=✅ 智能体已删除，绑定设备已解绑", status_code=303)
    except Exception as e:
        logger.error(f"delete agent failed #{agent_id}: {e}")
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ 删除失败: {esc(str(e))}", status_code=303)


@router.post("/admin/agent/{agent_id}/test")
async def admin_agent_test(agent_id: int, org_id: int = Query(0)):
    """测试智能体连接（真实调用一次 LLM，验证 key/地址/模型）"""
    base = f"/admin/agents?org_id={org_id}" if org_id else "/admin/agents"
    try:
        agent = await agent_service.get_agent(agent_id)
        if not agent:
            return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ 智能体不存在", status_code=303)
        ok, result = await agent_service.test_connection(agent)
        if ok:
            return RedirectResponse(
                url=f"{base}{'&' if '?' in base else '?'}msg=✅ 连接成功，模型回复：{esc(result[:80])}",
                status_code=303,
            )
        return RedirectResponse(
            url=f"{base}{'&' if '?' in base else '?'}msg=❌ 连接失败：{esc(result[:160])}", status_code=303
        )
    except Exception as e:
        logger.error(f"test agent failed #{agent_id}: {e}")
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ 测试异常: {esc(str(e))}", status_code=303)


# ════════════════════════════════════════════
#  智能体测试运行：模拟 incoming 消息，验证「智能体 → 工具调用 → 执行」全链路
# ════════════════════════════════════════════

_CMD_LABEL_MAP = {str(v): l for v, l in CMD_OPTIONS}


def _agent_tool_label(name: str, wf_names: dict) -> str:
    """把工具名转成人类可读标签（用于测试运行页的轨迹展示）"""
    if name.startswith("run_workflow_"):
        try:
            wid = int(name.split("_", 2)[2])
        except (IndexError, ValueError):
            wid = 0
        wname = wf_names.get(wid)
        return f"运行工作流 #{wid}" + (f"「{esc(wname)}」" if wname else "")
    if name.startswith("device_command_"):
        try:
            cmd = int(name.split("_", 2)[2])
        except (IndexError, ValueError):
            cmd = 0
        label = _CMD_LABEL_MAP.get(str(cmd), "")
        return f"设备指令 {cmd}" + (f" {esc(label)}" if label else "")
    if name == "http_get":
        return "HTTP GET（联网）"
    if name == "http_post":
        return "HTTP POST（联网）"
    if name == "table_query":
        return "查询表格"
    if name == "table_insert":
        return "写入表格"
    if name == "table_update":
        return "更新表格"
    if name == "task_create":
        return "创建任务"
    return esc(name)


@router.get("/admin/agent/{agent_id}/conversations/clear")
async def admin_agent_clear_conversations(agent_id: int, org_id: int = Query(0)):
    """清空该智能体的测试会话（合成 robot_id / 「智能体测试」房间），防止历史污染工具调用"""
    n = await agent_service.clear_test_conversations(agent_id)
    url = f"/admin/agents?org_id={org_id}&msg={quote(f'✅ 已清理 {n} 条测试会话')}"
    return RedirectResponse(url=url, status_code=303)


@router.get("/admin/agent/{agent_id}/test_run")
async def admin_agent_test_run_page(agent_id: int, org_id: int = Query(0)):
    """智能体测试运行表单页"""
    base = f"/admin/agents?org_id={org_id}" if org_id else "/admin/agents"
    agent = await agent_service.get_agent(agent_id)
    if not agent:
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ 智能体不存在", status_code=303)

    # 授权概览（仅同组织的工作流；设备指令不分组织）
    all_wfs = await workflow_service.list_workflows()
    wf_names = {w.id: w.name for w in all_wfs}
    sel_wf_ids = set(await agent_service.get_agent_workflows(agent_id))
    sel_cmd_ids = set(await agent_service.get_agent_commands(agent_id))
    org_wf_ids = None
    org_robot_ids = None
    if org_id:
        org_wf_ids = {int(x) for x in await group_service.get_group_org_members(org_id, "workflow")}
        org_robot_ids = set(await group_service.get_group_org_members(org_id, "device"))
    wf_list = [
        f"<code>#{wid}</code> {esc(wf_names.get(wid, ''))}"
        for wid in sorted(sel_wf_ids)
        if org_wf_ids is None or wid in org_wf_ids
    ]
    wf_html = "、".join(wf_list) if wf_list else '<span class="muted">（未授权工作流）</span>'
    cmd_list = [
        f"<code>{cid}</code> {esc(_CMD_LABEL_MAP.get(str(cid), ''))}"
        for cid in sorted(sel_cmd_ids)
    ]
    cmd_html = "、".join(cmd_list) if cmd_list else '<span class="muted">（未授权设备指令）</span>'

    # 设备下拉（仅同组织）
    all_robots = await robot_service.list_robots()
    robot_opts = ""
    for r in all_robots:
        if org_robot_ids is not None and str(r.robot_id) not in org_robot_ids:
            continue
        tag = "在线" if r.status == "online" else "离线"
        robot_opts += (
            f'<option value="{esc(r.robot_id)}">'
            f"{esc(r.robot_id)} ({esc(r.device_name or r.robot_id)}) [{tag}]</option>"
        )
    robot_opts = robot_opts or '<option value="">（暂无设备）</option>'

    oid_q = f"?org_id={org_id}" if org_id else ""
    tools_warn = (
        '<div class="note note-warn">⚠️ 该智能体未启用工具调用（历史数据，'
        "重新编辑保存一次后自动开启），LLM 不会获得任何工作流/设备指令工具，"
        "本次测试只会得到普通文本回复。</div>"
        if not agent.enable_tools
        else ""
    )
    content = f"""
    <div class="card">
        <div class="card-hd">
            <h3>🧪 智能体测试运行 <span class="muted" style="font-size:13px">#{agent.id} {esc(agent.name)}</span></h3>
            <a href="/admin/agents{oid_q}" class="btn btn-ghost btn-sm">← 返回智能体列表</a>
        </div>
        <p class="card-sub">
            模拟一条 incoming 消息走「设备 → 策略 → 智能体 → 工具调用 → 回复」全链路，
            验证智能体能否在合适时机调用<code>工作流</code>与<code>设备指令</code>并完成任务。
            区别于「测试」按钮（只验证连接），这里会真实调用 LLM 并展示工具调用轨迹。
        </p>
        <div class="stat-grid" style="margin-top:8px">
            <div class="stat-card"><div class="stat-label">服务商 / 模型</div><div class="stat-num" style="font-size:15px">{esc(agent.model)}</div></div>
            <div class="stat-card"><div class="stat-label">工具调用</div><div class="stat-num" style="font-size:15px">{'✅ 启用' if agent.enable_tools else '❌ 未启用'}</div></div>
            <div class="stat-card"><div class="stat-label">授权工作流</div><div class="stat-num" style="font-size:12px">{wf_html}</div></div>
            <div class="stat-card"><div class="stat-label">授权设备指令</div><div class="stat-num" style="font-size:12px">{cmd_html}</div></div>
            <div class="stat-card"><div class="stat-label">创建任务</div><div class="stat-num" style="font-size:15px">{'✅ 允许' if getattr(agent, 'allow_task_create', False) else '❌ 未授权'}</div></div>
        </div>
        {tools_warn}
    </div>
    <div class="card">
        <h3>▶️ 运行测试</h3>
        <form method="POST" action="/admin/agent/{agent_id}/test_run{oid_q}" class="form-row">
            <div class="form-group"><label>绑定设备（可选）</label>
                <select name="robot_id" style="min-width:220px">
                    <option value="">（不指定，自动使用模拟模式）</option>{robot_opts}
                </select></div>
            <div class="form-group" style="flex:1"><label>模拟输入消息（选填，留空=无用户消息）</label>
                <input type="text" name="user_text" value="帮我查一下订单 SO123 的物流"
                       placeholder="留空可测试无用户消息场景（工作流可不依赖 user_text）"></div>
            <div class="form-group"><label>虚拟设备</label>
                <label class="form-check" style="margin-top:8px">
                    <input type="checkbox" name="simulate" value="1" checked>
                    <span>勾选后设备指令/工作流设备节点不真实执行；未选择设备时自动使用模拟模式</span>
                </label>
            </div>
            <button class="btn btn-success">🚀 执行测试</button>
            <a class="btn" href="/admin/agent/{agent_id}/conversations/clear{oid_q}"
               onclick="return confirm('确认清空该智能体的所有测试会话？（含合成会话与「智能体测试」房间）')"
               style="background:#f1f2f6;color:#b91c1c;border:1px solid #e5e7eb">🧹 清空测试会话</a>
        </form>
    </div>
    <div class="note note-info">
        <b>判定标准：</b>① 看下方「工具调用轨迹」——LLM 是否在合适时机调用了 <code>run_workflow_*</code> / <code>device_command_*</code>；
        ② 看「最终回复」——是否基于工具执行结果正确作答；③ 模拟模式下设备指令只校验不真实下发，工作流设备节点返回虚拟数据；
        勾选真实设备并取消模拟后才会向设备真实下发指令（需设备在线）。
    </div>
    """
    return page("智能体测试运行", "🤖 智能体测试运行", content, active="agents", org_id=org_id)


@router.post("/admin/agent/{agent_id}/test_run")
async def admin_agent_test_run(
    agent_id: int,
    user_text: str = Form(""),
    robot_id: str = Form(""),
    simulate: bool = Form(False),
    org_id: int = Query(0),
):
    """执行智能体测试运行：调用 generate_reply（带工具+模拟+轨迹），渲染结果页"""
    base = f"/admin/agents?org_id={org_id}" if org_id else "/admin/agents"
    agent = await agent_service.get_agent(agent_id)
    if not agent:
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=❌ 智能体不存在", status_code=303)

    # 未选择设备时自动降级为模拟模式（否则设备指令/工作流设备节点无法执行）
    simulate = bool(simulate) or not robot_id
    test_robot = robot_id or f"test{agent_id}"  # 必须 ≤10 字符，否则超出 agent_conversations.robot_id VARCHAR(10)
    user_text = (user_text or "").strip()

    all_wfs = await workflow_service.list_workflows()
    wf_names = {w.id: w.name for w in all_wfs}

    trace: list = []
    error = ""
    if not user_text:
        # 空消息：智能体仅在收到用户消息时生成回复，本次不调用 LLM
        error = "未输入模拟消息：智能体仅在收到用户消息时生成回复，本次未调用 LLM"
        reply = None
    else:
        try:
            reply = await agent_service.generate_reply(
                agent,
                robot_id=test_robot,
                room_name="智能体测试",
                room_type=2,
                user_text=user_text,
                simulate=simulate,
                trace=trace,
                org_id=org_id,
            )
        except Exception as e:
            logger.error(f"agent test_run failed #{agent_id}: {e}")
            reply = None
            error = str(e)

    if reply:
        status_html = '<span class="badge badge-online">✅ 成功</span>'
    else:
        status_html = f'<span class="badge badge-offline">❌ 失败{(": " + esc(error[:160])) if error else "（LLM 未返回内容，详见服务端日志）"}</span>'

    # 工具调用轨迹
    trace_rows = ""
    for i, t in enumerate(trace, 1):
        args_txt = json.dumps(t.get("args") or {}, ensure_ascii=False)
        result_txt = str(t.get("result") or "")
        args_show = args_txt[:400] + ("…" if len(args_txt) > 400 else "")
        result_show = result_txt[:400] + ("…" if len(result_txt) > 400 else "")
        sim_badge = ' <span class="badge badge-blue">模拟</span>' if "模拟" in result_txt else ""
        trace_rows += (
            f"<tr>"
            f"<td>{i}</td>"
            f"<td><code>{_agent_tool_label(t.get('tool') or '', wf_names)}</code></td>"
            f"<td class='mono' style='max-width:280px;font-size:12px'>{esc(args_show)}</td>"
            f"<td class='mono' style='max-width:320px;font-size:12px'>{esc(result_show)}{sim_badge}</td>"
            f"</tr>"
        )
    if not trace:
        if agent.enable_tools:
            trace_rows = (
                '<tr><td colspan="4" class="empty">LLM 未调用任何工具'
                "（可能认为无需工具即可回答，可在系统提示词中引导它使用工作流/设备指令）</td></tr>"
            )
        else:
            trace_rows = (
                '<tr><td colspan="4" class="empty">该智能体未启用工具调用'
                '（历史数据，重新编辑保存一次后自动开启）</td></tr>'
            )
    tool_count = len(trace)

    warnings = ""
    if not agent.enable_tools:
        warnings += (
            '<div class="note note-warn">⚠️ 该智能体未启用工具调用'
            '（历史数据，重新编辑保存一次后自动开启），'
            "本次测试 LLM 不会获得工作流/设备指令工具。</div>"
        )
    if not user_text:
        warnings += (
            '<div class="note note-warn">⚠️ 本次未输入模拟消息，未调用 LLM。'
            "智能体仅在收到用户消息时生成回复；若需验证「工作流不依赖用户输入」，"
            "请直接用该工作流详情页的「测试运行」执行。</div>"
        )
    if simulate:
        warnings += (
            '<div class="note note-info">ℹ 本次为<b>模拟模式</b>：设备指令只校验不真实下发，'
            "工作流内设备指令节点返回虚拟数据（<code>simulated=True</code>）。</div>"
        )
    else:
        warnings += (
            '<div class="note note-info">ℹ 本次为<b>真实模式</b>：'
            "设备指令将真实下发到所选设备，工作流设备节点等待设备回报。</div>"
        )

    oid_q = f"?org_id={org_id}" if org_id else ""
    content = f"""
    <div class="card">
        <div class="card-hd">
            <h3>🧪 测试运行结果 <span class="muted" style="font-size:13px">#{agent.id} {esc(agent.name)}</span></h3>
            <span>
                <a href="/admin/agent/{agent_id}/test_run{oid_q}" class="btn btn-ghost btn-sm">↻ 再次测试</a>
                <a href="/admin/agents{oid_q}" class="btn btn-ghost btn-sm">← 返回列表</a>
            </span>
        </div>
        <div class="stat-grid" style="margin-top:4px">
            <div class="stat-card"><div class="stat-label">状态</div><div style="margin-top:6px">{status_html}</div></div>
            <div class="stat-card"><div class="stat-label">模式</div><div class="stat-num" style="font-size:20px">{'模拟' if simulate else '真实'}</div></div>
            <div class="stat-card"><div class="stat-label">设备</div><div class="stat-num" style="font-size:20px">{esc(robot_id) or '—'}</div></div>
            <div class="stat-card"><div class="stat-label">工具调用</div><div class="stat-num" style="font-size:20px">{tool_count} 次</div></div>
        </div>
        <p class="card-sub" style="margin-top:14px"><b>模拟输入</b>：<code>{esc(user_text)}</code></p>
        {warnings}
    </div>
    <div class="card">
        <h3>🔧 工具调用轨迹（LLM 决策过程）</h3>
        <div class="table-wrap">
        <table>
            <thead><tr><th>#</th><th>工具</th><th>参数</th><th>执行结果（回传给 LLM）</th></tr></thead>
            <tbody>{trace_rows}</tbody>
        </table>
        </div>
    </div>
    <div class="card">
        <h3>💬 智能体最终回复</h3>
        <pre class="mono" style="white-space:pre-wrap;word-break:break-all;background:#f8fafc;padding:12px;border-radius:6px;font-size:14px">{esc(reply) if reply else '<span style="color:#dc2626">（无回复）</span>'}</pre>
    </div>
    """
    return page("测试运行结果", "🧪 智能体测试运行", content, active="agents", org_id=org_id)
