"""
知识库（Knowledge Base）管理页面（服务端渲染）

支持两种知识库：
- 本地知识库（local）：上传 txt/md/pdf/docx 文档，自动切分；检索可用关键词（默认）或 Embedding（OpenAI 兼容）
- 阿里云百炼云知识库（bailian）：基于百炼控制台上传的知识库，调用 Retriever API 检索
- 自定义 HTTP 检索接口（http）：POST {"query","top_k"} 获取文本片段

智能体在编辑页可勾选引用一个或多个知识库（agent_knowledge 多对多），
收到消息时自动检索并注入 prompt（RAG）。
"""

import json
import logging
from typing import List

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse

from ..services import knowledge_service, group_service
from ..layout import page, esc

logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge"])

KB_TYPE_LABELS = {
    "local": "本地知识库",
    "bailian": "阿里云百炼云知识库",
    "http": "自定义 HTTP 检索接口",
}

# 常用 Embedding 服务预设 (base_url, model, 显示名)
# 地址必须是 OpenAI 兼容根地址：服务端会请求 {base_url}/embeddings
EMBEDDING_PRESETS = (
    ("http://localhost:11434/v1", "bge-m3", "本地 Ollama · bge-m3（多语言，1024 维）"),
    ("http://localhost:11434/v1", "qwen3-embedding:0.6b", "本地 Ollama · qwen3-embedding:0.6b（轻量）"),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "text-embedding-v3", "阿里云百炼 · text-embedding-v3"),
    ("https://api.openai.com/v1", "text-embedding-3-small", "OpenAI · text-embedding-3-small"),
)


def _kb_config_form(kb_type: str, cfg: dict, is_edit: bool = False) -> str:
    """按类型渲染配置区（JS 按类型切换显示）"""
    # 本地库
    local_retrieval = cfg.get("retrieval") or "keyword"
    emb_display = "grid" if local_retrieval == "embedding" else "none"
    preset_opts = "".join(
        f'<option value="{url}|{model}">{esc(label)}</option>'
        for url, model, label in EMBEDDING_PRESETS
    )
    local_html = f"""
    <div class="f-cfg f-cfg-local">
        <div class="form-group full">
            <label>本地检索方式</label>
            <select name="retrieval" class="f-retrieval" onchange="onRetrievalChange(this)">
                <option value="keyword"{(" selected" if local_retrieval == "keyword" else "")}>关键词检索（2-gram，零依赖，默认）</option>
                <option value="local"{(" selected" if local_retrieval == "local" else "")}>本地Embedding（内置零依赖哈希向量）</option>
                <option value="embedding"{(" selected" if local_retrieval == "embedding" else "")}>Embedding 向量检索（需配置以下 Embedding API）</option>
            </select>
        </div>
        <div class="f-emb-fields" style="display:{emb_display};grid-column:1/-1;grid-template-columns:repeat(3,1fr);gap:14px 16px">
            <div class="form-group full" style="grid-column:1/-1">
                <label>常用 Embedding 服务（选中自动填地址与模型，仍可手改）</label>
                <select class="f-emb-preset" onchange="onEmbPresetChange(this)">
                    <option value="">— 手动填写 —</option>
                    {preset_opts}
                </select>
            </div>
            <div class="form-group"><label>Embedding API 地址</label><input type="text" class="f-emb-url" name="embedding_base_url" value="{esc(cfg.get('embedding_base_url', ''))}" placeholder="如 http://localhost:11434/v1（Ollama）"></div>
            <div class="form-group"><label>Embedding API Key</label><input type="password" name="embedding_api_key" value="" placeholder="{'留空保持原 Key' if is_edit else '本地 Ollama 可留空'}"></div>
            <div class="form-group"><label>Embedding 模型</label><input type="text" class="f-emb-model" name="embedding_model" value="{esc(cfg.get('embedding_model', 'text-embedding-v3'))}" placeholder="bge-m3"></div>
        </div>
    </div>"""
    # 百炼
    bailian_key_req = "" if is_edit else ' data-req="1"'
    bailian_key_ph = "留空保持原 Key" if is_edit else "sk-..."
    bailian_html = f"""
    <div class="f-cfg f-cfg-bailian" style="display:none">
        <div class="form-group"><label>百炼 API 地址（可留空用默认）</label><input type="text" name="bailian_base_url" value="{esc(cfg.get('base_url', ''))}" placeholder="https://dashscope.aliyuncs.com"></div>
        <div class="form-group"><label>百炼 API Key *</label><input type="password" name="bailian_api_key" value="" placeholder="{bailian_key_ph}"{bailian_key_req}></div>
        <div class="form-group"><label>Workspace ID（百炼控制台查询）</label><input type="text" name="workspace_id" value="{esc(cfg.get('workspace_id', ''))}" placeholder="若控制台配置了工作空间需填写"></div>
        <div class="form-group"><label>知识库名称（collection_name，即向量存储名称）*</label><input type="text" name="collection_name" value="{esc(cfg.get('collection_name', ''))}" placeholder="控制台知识库的名称" data-req="1"></div>
    </div>"""
    # HTTP
    http_html = f"""
    <div class="f-cfg f-cfg-http" style="display:none">
        <div class="form-group full"><label>检索接口地址 *（POST JSON {"{"}query, top_k{"}"}）</label><input type="text" name="http_url" value="{esc(cfg.get('url', ''))}" placeholder="https://your-service/api/kb/search" data-req="1"></div>
        <div class="form-group"><label>接口鉴权 Key（可选，Bearer 方式）</label><input type="password" name="http_api_key" value="" placeholder="{'留空保持原 Key' if is_edit else '留空则不带鉴权头'}"></div>
    </div>"""
    return local_html + bailian_html + http_html


def _kb_form(
    action: str,
    v: dict = None,
    is_edit: bool = False,
) -> str:
    """渲染知识库表单（新建/编辑共用）。v 为预填 dict。"""
    v = v or {}
    name = esc(v.get("name", ""))
    description = esc(v.get("description", ""))
    kb_type = v.get("kb_type", "local")
    cfg = v.get("config") or {}
    chunk_size = v.get("chunk_size", 500)
    chunk_overlap = v.get("chunk_overlap", 50)
    top_k = v.get("top_k", 3)
    enabled = bool(v.get("enabled", True))
    checked = " checked" if enabled else ""

    type_opts = "".join(
        f'<option value="{t}"{" selected" if t == kb_type else ""}>{KB_TYPE_LABELS.get(t, t)}</option>'
        for t in ("local", "bailian", "http")
    )

    return f"""
    <form method="POST" action="{action}" class="form-stack" data-kb-form>
        <div class="form-grid-3">
            <div class="form-group"><label>知识库名称 *</label><input type="text" name="name" value="{name}" required></div>
            <div class="form-group"><label>类型</label><select name="kb_type" class="f-kbtype" onchange="onKbTypeChange(this)">{type_opts}</select></div>
            <div class="form-group"><label>描述</label><input type="text" name="description" value="{description}" placeholder="用途说明"></div>
        </div>
        <div class="form-grid-3">
            <div class="form-group"><label>切分长度（字符）</label><input type="number" name="chunk_size" value="{chunk_size}" min="50"></div>
            <div class="form-group"><label>切分重叠</label><input type="number" name="chunk_overlap" value="{chunk_overlap}" min="0"></div>
            <div class="form-group"><label>每次检索返回片段数 top_k</label><input type="number" name="top_k" value="{top_k}" min="1" max="20"></div>
        </div>
        <div class="form-grid-3">
            {_kb_config_form(kb_type, cfg, is_edit)}
        </div>
        <div class="inline-flex" style="gap:16px">
            <label class="form-check"><input type="checkbox" name="enabled"{checked}> 启用（停用则不参与检索）</label>
        </div>
        <div><button type="submit" class="btn btn-primary">保存</button></div>
    </form>"""


async def _collect_kb_form(
    name, kb_type, description, chunk_size, chunk_overlap, top_k,
    enabled, retrieval, embedding_base_url, embedding_api_key, embedding_model,
    bailian_base_url, bailian_api_key, workspace_id, collection_name,
    http_url, http_api_key,
) -> dict:
    """组装 config dict"""
    config: dict = {}
    if kb_type == "local":
        config["retrieval"] = retrieval or "keyword"
        if config["retrieval"] == "embedding":
            config["embedding_base_url"] = embedding_base_url or ""
            config["embedding_api_key"] = embedding_api_key or ""
            config["embedding_model"] = embedding_model or "text-embedding-v3"
    elif kb_type == "bailian":
        config["base_url"] = bailian_base_url or ""
        config["api_key"] = bailian_api_key or ""
        config["workspace_id"] = workspace_id or ""
        config["collection_name"] = collection_name or ""
    elif kb_type == "http":
        config["url"] = http_url or ""
        config["api_key"] = http_api_key or ""
    return config


@router.get("/admin/knowledge")
async def admin_knowledge(msg: str = "", view: int = 0, org_id: int = 0):
    """知识库管理页（支持 ?org_id= 按组织过滤）"""
    all_kbs = await knowledge_service.list_kbs()
    cur_org_name = ""
    if org_id:
        grp = await group_service.get_org_group(org_id)
        if not grp:
            return RedirectResponse(
                url="/admin/orgs?msg=❌ 组织不存在", status_code=303
            )
        cur_org_name = grp["name"]
        member_ids = set(grp["members"].get("knowledge", []))
        all_kbs = [kb for kb in all_kbs if str(kb.id) in member_ids]

    kbs = all_kbs
    kb_groups = await group_service.list_org_groups()
    kb_group_name = {g["id"]: g["name"] for g in kb_groups}

    rows_parts = []
    for kb in kbs:
        sel_groups = set(await group_service.get_member_org_groups("knowledge", kb.id))
        kb_cfg = kb.config or {}
        type_label = KB_TYPE_LABELS.get(kb.kb_type, kb.kb_type)
        type_desc = {
            "local": "上传文档，本地检索",
            "bailian": "阿里云百炼 Retriever",
            "http": "自定义 HTTP 接口",
        }.get(kb.kb_type, "")
        retrieval_label = (
            "Embedding" if kb_cfg.get("retrieval") == "embedding" else "关键词"
        )
        agent_cnt = await knowledge_service.get_kb_agent_count(kb.id)
        status_badge = (
            '<span class="badge badge-online">启用</span>'
            if kb.enabled
            else '<span class="badge badge-offline">停用</span>'
        )
        rows_parts.append(
            f"<tr>"
            f"<td><code>#{kb.id}</code></td>"
            f"<td><span class='cell-main'>{esc(kb.name)}</span><span class='cell-sub'>{esc(kb.description)}</span></td>"
            f"<td>{esc(type_label)}<span class='cell-sub'>{esc(type_desc)}</span></td>"
            f"<td>top_k={kb.top_k}<br>{esc(retrieval_label)}</td>"
            f"<td>{kb.doc_count} 篇<br>{kb.chunk_count} 分片</td>"
            f"<td>{agent_cnt} 个智能体</td>"
            f"<td>{'、'.join(esc(kb_group_name[gid]) for gid in sorted(sel_groups) if gid in kb_group_name) or '—'}</td>"
            f"<td>{status_badge}</td>"
            f"<td>"
            f"<a href='/admin/knowledge?view={kb.id}{f'&org_id={org_id}' if org_id else ''}' class='btn btn-ghost btn-sm'>详情</a> "
            f"<button class='btn btn-primary btn-sm' onclick=\"toggleEdit({kb.id})\">编辑</button> "
            f"<a href='/admin/knowledge/{kb.id}/delete{f'?org_id={org_id}' if org_id else ''}' class='btn btn-danger btn-sm' onclick=\"return confirm('删除知识库「{esc(kb.name)}」？相关文档、分片及智能体引用将一并删除！')\">删除</a>"
            f"</td>"
            f"</tr>"
            f"<tr id='edit-{kb.id}' style='display:none;background:#f8fafc'>"
            f"<td colspan='9'>"
            f"{_kb_form(f'/admin/knowledge/{kb.id}/update?org_id={org_id}' if org_id else f'/admin/knowledge/{kb.id}/update', kb.__dict__ if hasattr(kb, '__dict__') else {'name': kb.name, 'kb_type': kb.kb_type, 'description': kb.description, 'config': kb.config, 'chunk_size': kb.chunk_size, 'chunk_overlap': kb.chunk_overlap, 'top_k': kb.top_k, 'enabled': kb.enabled}, is_edit=True)}"
            f"</td></tr>"
        )
    rows_html = (
        "\n".join(rows_parts)
        if rows_parts
        else '<tr><td colspan="9" class="empty">暂无知识库，先在上方创建</td></tr>'
    )

    table_html = f"""
    <div class="table-wrap">
    <table>
        <thead><tr><th>ID</th><th>名称</th><th>类型</th><th>检索</th><th>文档 / 分片</th><th>被引用</th><th>组织</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>{rows_html}</tbody>
    </table>
    </div>"""

    note_html = """
    <div class="note note-info">
        <b>什么是知识库：</b>为智能体提供外部参考资料（RAG）。设备收到消息后，服务器先从智能体绑定的知识库中检索相关内容，连同提示词一起发给 LLM，使回复基于你的资料而非凭空编造。<br>
        <b>本地知识库：</b>支持上传 <code>txt / md / csv / json / pdf / docx</code> 文档，自动按「切分长度」切分为片段入库。检索方式三种：<b>关键词</b>（2-gram，零依赖、无需联网）、<b>本地Embedding</b>（内置零依赖哈希向量，无需 API/模型，离线可用，语义略强于关键词）、<b>Embedding 向量检索</b>（配置 OpenAI 兼容的 embedding 接口，如百炼 <code>text-embedding-v3</code>、OpenAI <code>text-embedding-3-small</code>，或本地部署的 Ollama/Xinference 等服务），上传文档时自动向量化，检索按余弦相似度排序。<br>
        <b>阿里云百炼云知识库：</b>在百炼控制台上传文档并创建知识库后，在此填入 <code>API Key / Workspace ID / 知识库名称</code>，回复时调用百炼 Retriever API 检索，文档维护全在云端。<br>
        <b>自定义 HTTP：</b>对接自建检索服务（如企业内部搜索、ES 等），服务器向该地址 POST <code>{"{query, top_k}"}</code>，从响应中提取文本片段。<br>
        <b>使用方式：</b>先在「智能体」页编辑智能体，勾选要引用的知识库（可多选）。回复时最多注入 <code>2000</code> 字符的参考资料，防止 prompt 过长。
    </div>"""

    # ── 详情：本地库文档上传 / 测试检索 ──
    detail_html = ""
    if view:
        kb = await knowledge_service.get_kb(view)
        if not kb:
            detail_html = '<div class="msg msg-error">知识库不存在</div>'
        else:
            # 云知识库（bailian/http）的文档在平台侧，本地没有副本：
            # 既不该列出空文档表（会被误读成"库里没东西"），也不该给上传入口
            is_local = kb.kb_type == "local"
            docs = await knowledge_service.list_documents(view) if is_local else []
            doc_rows = ""
            if docs:
                doc_rows = "".join(
                    f"<tr><td><code>#{d['id']}</code></td><td>{esc(d['filename'])}</td>"
                    f"<td>{d['chunk_count']} 分片</td>"
                    f"<td>{esc(d['created_at'][:19])}</td>"
                    f"<td><a href='/admin/knowledge/doc/{d['id']}/delete?view={view}{f'&org_id={org_id}' if org_id else ''}' class='btn btn-danger btn-sm' onclick=\"return confirm('删除文档「{esc(d['filename'])}」？')\">删除</a></td></tr>"
                    for d in docs
                )
            else:
                doc_rows = '<tr><td colspan="5" class="empty">暂无文档，请上传</td></tr>'
            docs_block = (
                f'<div class="table-wrap"><table>'
                f'<thead><tr><th>ID</th><th>文件名</th><th>分片</th><th>上传时间</th><th>操作</th></tr></thead>'
                f'<tbody>{doc_rows}</tbody></table></div>'
                if is_local
                else '<p class="card-sub">☁️ 这是云知识库，文档在对应平台侧维护'
                     '（阿里云百炼控制台 / 自定义检索接口），本地不保存副本，'
                     '因此这里不显示文档列表，也没有上传入口。</p>'
            )
            upload_area = ""
            if kb.kb_type == "local":
                upload_area = f"""
                <div class="dropzone">
                    <form method="POST" action="/admin/knowledge/{view}/upload{f'?org_id={org_id}' if org_id else ''}" enctype="multipart/form-data" class="inline-flex" style="gap:12px">
                        <input type="file" name="files" multiple accept=".txt,.md,.csv,.json,.log,.pdf,.docx" style="font-size:13px;width:auto;max-width:320px">
                        <button type="submit" class="btn btn-success">上传并切分</button>
                        <span class="form-tip">支持 txt/md/csv/json/log/pdf/docx</span>
                    </form>
                </div>"""
            detail_html = f"""
            <div class="card">
                <h3>📁 知识库「{esc(kb.name)}」详情</h3>
                <p class="card-sub">类型：{KB_TYPE_LABELS.get(kb.kb_type, kb.kb_type)} · 切分 {kb.chunk_size} 字符 / 重叠 {kb.chunk_overlap} · top_k={kb.top_k} · {kb.doc_count} 篇文档 / {kb.chunk_count} 分片</p>
                {upload_area}
                {docs_block}
            </div>"""

    test_html = f"""
    <div class="card">
        <h3>🔍 测试检索（验证知识库命中效果）</h3>
        <form method="POST" action="/admin/knowledge/{view}/test{f'?org_id={org_id}' if org_id else ''}" class="inline-flex" style="gap:12px" onsubmit="document.getElementById('kb-test-msg').textContent='';">
            <input type="text" name="query" placeholder="输入测试问题，如：我们的退货政策是什么？" required style="width:min(420px,100%)">
            <button type="submit" class="btn btn-primary">检索</button>
            <span class="form-tip">返回每个命中片段的前 160 字预览</span>
        </form>
    </div>"""

    content = f"""
    <div class="card">
        <h3>📚 新建知识库</h3>
        <p class="card-sub">本地（上传文档）或云端（阿里云百炼 / 自定义 HTTP 检索接口）。创建后在「智能体」页为智能体勾选引用。</p>
        {note_html}
        {_kb_form(f"/admin/knowledge/create?org_id={org_id}" if org_id else "/admin/knowledge/create")}
    </div>
    <div class="card">
        <h3>🗂 知识库列表</h3>
        {table_html}
    </div>
    {detail_html}
    {test_html if view else ""}
    <script>
    function onKbTypeChange(sel){{
        var form = sel.closest('form');
        var t = sel.value;
        ['local','bailian','http'].forEach(function(x){{
            var el = form.querySelector('.f-cfg-' + x);
            if (el) el.style.display = (x === t) ? '' : 'none';
            // required 只在当前可见的配置区生效，避免浏览器校验隐藏区块
            if (el) el.querySelectorAll('[data-req]').forEach(function(inp){{
                inp.required = (x === t);
            }});
        }});
    }}
    function onRetrievalChange(sel){{
        var form = sel.closest('form');
        var emb = form.querySelector('.f-emb-fields');
        if (emb) emb.style.display = (sel.value === 'embedding') ? 'grid' : 'none';
    }}
    function onEmbPresetChange(sel){{
        if (!sel.value) return;
        var form = sel.closest('form');
        var url = form.querySelector('.f-emb-url'), model = form.querySelector('.f-emb-model');
        var parts = sel.value.split('|');
        if (url) url.value = parts[0];
        if (model) model.value = parts[1];
    }}
    document.addEventListener('DOMContentLoaded', function(){{
        document.querySelectorAll('form[data-kb-form]').forEach(function(form){{
            var sel = form.querySelector('.f-kbtype');
            if (sel) onKbTypeChange(sel);
            var rs = form.querySelector('.f-retrieval');
            if (rs) onRetrievalChange(rs);
            // 回显：当前地址+模型正好是某个预设时选中它，否则留「手动填写」
            var ep = form.querySelector('.f-emb-preset');
            if (ep) {{
                var u = form.querySelector('.f-emb-url'), m = form.querySelector('.f-emb-model');
                var key = u && m ? (u.value.trim() + '|' + m.value.trim()) : '';
                var hit = Array.prototype.some.call(ep.options, function(o){{ return o.value === key; }});
                ep.value = hit ? key : '';
            }}
        }});
    }});
    function toggleEdit(id){{
        var el = document.getElementById('edit-' + id);
        if (el) el.style.display = (el.style.display === 'none') ? '' : 'none';
    }}
    </script>"""

    return page(
        "知识库", "📚 知识库管理", content, msg, active="knowledge",
        org_id=org_id, org_name=cur_org_name, org_active="knowledge",
    )


# ── POST / GET 操作 ──────────────────────────────────


@router.post("/admin/knowledge/create")
async def admin_kb_create(
    org_id: int = Query(0),
    name: str = Form(...),
    kb_type: str = Form("local"),
    description: str = Form(""),
    chunk_size: int = Form(500),
    chunk_overlap: int = Form(50),
    top_k: int = Form(3),
    enabled: bool = Form(False),
    retrieval: str = Form("keyword"),
    embedding_base_url: str = Form(""),
    embedding_api_key: str = Form(""),
    embedding_model: str = Form("text-embedding-v3"),
    bailian_base_url: str = Form(""),
    bailian_api_key: str = Form(""),
    workspace_id: str = Form(""),
    collection_name: str = Form(""),
    http_url: str = Form(""),
    http_api_key: str = Form(""),
):
    try:
        config = await _collect_kb_form(
            name, kb_type, description, chunk_size, chunk_overlap, top_k, enabled,
            retrieval, embedding_base_url, embedding_api_key, embedding_model,
            bailian_base_url, bailian_api_key, workspace_id, collection_name,
            http_url, http_api_key,
        )
        kb_id = await knowledge_service.create_kb(
            name, kb_type, description, config, chunk_size, chunk_overlap, top_k
        )
        # 在组织上下文中创建则自动归属该组织（创建后不可改）
        if org_id:
            await group_service.set_member_org_groups("knowledge", kb_id, [org_id])
        base = f"/admin/knowledge?org_id={org_id}" if org_id else "/admin/knowledge"
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=✅ 知识库已创建&view={kb_id}", status_code=303)
    except Exception as e:
        logger.error(f"create kb failed: {e}")
        return RedirectResponse(url=f"/admin/knowledge?msg=❌ {esc(str(e))}", status_code=303)


@router.post("/admin/knowledge/{kb_id}/update")
async def admin_kb_update(
    kb_id: int,
    org_id: int = Query(0),
    name: str = Form(...),
    kb_type: str = Form("local"),
    description: str = Form(""),
    chunk_size: int = Form(500),
    chunk_overlap: int = Form(50),
    top_k: int = Form(3),
    enabled: bool = Form(False),
    retrieval: str = Form("keyword"),
    embedding_base_url: str = Form(""),
    embedding_api_key: str = Form(""),
    embedding_model: str = Form("text-embedding-v3"),
    bailian_base_url: str = Form(""),
    bailian_api_key: str = Form(""),
    workspace_id: str = Form(""),
    collection_name: str = Form(""),
    http_url: str = Form(""),
    http_api_key: str = Form(""),
):
    try:
        kb = await knowledge_service.get_kb(kb_id)
        if not kb:
            return RedirectResponse(url="/admin/knowledge?msg=❌ 知识库不存在", status_code=303)
        old_cfg = kb.config or {}
        config = await _collect_kb_form(
            name, kb_type, description, chunk_size, chunk_overlap, top_k, enabled,
            retrieval,
            embedding_base_url or old_cfg.get("embedding_base_url", ""),
            embedding_api_key or old_cfg.get("embedding_api_key", ""),
            embedding_model or old_cfg.get("embedding_model", "text-embedding-v3"),
            bailian_base_url or old_cfg.get("base_url", ""),
            bailian_api_key or old_cfg.get("api_key", ""),
            workspace_id or old_cfg.get("workspace_id", ""),
            collection_name or old_cfg.get("collection_name", ""),
            http_url or old_cfg.get("url", ""),
            http_api_key or old_cfg.get("api_key", ""),
        )
        await knowledge_service.update_kb(
            kb_id, name, kb_type, description, config,
            chunk_size, chunk_overlap, top_k, enabled,
        )
        base = f"/admin/knowledge?org_id={org_id}" if org_id else "/admin/knowledge"
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg=✅ 知识库已更新&view={kb_id}", status_code=303)
    except Exception as e:
        logger.error(f"update kb failed #{kb_id}: {e}")
        return RedirectResponse(url=f"/admin/knowledge?msg=❌ {esc(str(e))}", status_code=303)


@router.get("/admin/knowledge/{kb_id}/delete")
async def admin_kb_delete(kb_id: int, org_id: int = Query(0)):
    try:
        ok = await knowledge_service.delete_kb(kb_id)
        base = f"/admin/knowledge?org_id={org_id}" if org_id else "/admin/knowledge"
        return RedirectResponse(
            url=f"{base}{'&' if '?' in base else '?'}msg={'✅ 知识库已删除' if ok else '❌ 知识库不存在'}",
            status_code=303,
        )
    except Exception as e:
        logger.error(f"delete kb failed #{kb_id}: {e}")
        return RedirectResponse(url=f"/admin/knowledge?msg=❌ 删除失败: {esc(str(e))}", status_code=303)


@router.post("/admin/knowledge/{kb_id}/upload")
async def admin_kb_upload(kb_id: int, files: List[UploadFile] = File(...), org_id: int = Query(0)):
    try:
        total_chunks = 0
        errors = []
        for f in files:
            try:
                data = await f.read()
                chunks = await knowledge_service.upload_document(kb_id, f.filename, data)
                total_chunks += chunks
            except Exception as e:
                errors.append(f"{f.filename}: {e}")
        msg = f"✅ 上传完成，共新增 {total_chunks} 个分片"
        if errors:
            msg += "；失败：" + "；".join(errors[:3])
        base = f"/admin/knowledge?org_id={org_id}" if org_id else "/admin/knowledge"
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg={msg}&view={kb_id}", status_code=303)
    except Exception as e:
        logger.error(f"upload kb #{kb_id} failed: {e}")
        return RedirectResponse(url=f"/admin/knowledge?msg=❌ 上传失败: {esc(str(e))}&view={kb_id}", status_code=303)


@router.get("/admin/knowledge/doc/{doc_id}/delete")
async def admin_doc_delete(doc_id: int, view: int = 0, org_id: int = Query(0)):
    try:
        ok = await knowledge_service.delete_document(doc_id)
        base = f"/admin/knowledge?view={view}&org_id={org_id}" if org_id else f"/admin/knowledge?view={view}"
        return RedirectResponse(
            url=f"{base}{'&' if '?' in base else '?'}msg={'✅ 文档已删除' if ok else '❌ 文档不存在'}",
            status_code=303,
        )
    except Exception as e:
        logger.error(f"delete doc failed #{doc_id}: {e}")
        return RedirectResponse(url=f"/admin/knowledge?view={view}&msg=❌ 删除失败: {esc(str(e))}", status_code=303)


@router.post("/admin/knowledge/{kb_id}/test")
async def admin_kb_test(kb_id: int, query: str = Form(...), org_id: int = Query(0)):
    """测试检索，展示命中片段预览"""
    try:
        kb = await knowledge_service.get_kb(kb_id)
        if not kb:
            base = f"/admin/knowledge?org_id={org_id}" if org_id else "/admin/knowledge"
            return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}view={kb_id}&msg=❌ 知识库不存在", status_code=303)
        hits = await knowledge_service.retrieve(kb, query, top_k=kb.top_k or 3)
        if not hits:
            msg = f"ℹ 未命中与「{query}」相关的内容"
        else:
            previews = []
            for i, h in enumerate(hits, 1):
                previews.append(f"{i}. {h[:160]}")
            msg = f"✅ 命中 {len(hits)} 条：{esc(' || '.join(previews))[:1200]}"
        base = f"/admin/knowledge?view={kb_id}&org_id={org_id}" if org_id else f"/admin/knowledge?view={kb_id}"
        return RedirectResponse(url=f"{base}{'&' if '?' in base else '?'}msg={msg}", status_code=303)
    except Exception as e:
        logger.error(f"test kb #{kb_id} failed: {e}")
        return RedirectResponse(url=f"/admin/knowledge?view={kb_id}&msg=❌ 检索失败: {esc(str(e))}", status_code=303)
