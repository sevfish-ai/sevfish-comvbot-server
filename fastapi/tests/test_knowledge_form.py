"""知识库表单：Embedding 常用服务预设（纯渲染，不连库）。"""

from app.routes.knowledge import EMBEDDING_PRESETS, _kb_config_form


def test_embedding_presets_in_form():
    html = _kb_config_form("local", {"retrieval": "embedding"})
    assert 'class="f-emb-preset"' in html
    assert 'value=""' in html  # 「— 手动填写 —」
    for url, model, _label in EMBEDDING_PRESETS:
        assert f'value="{url}|{model}"' in html
    # 两个本地 Ollama 常用项
    assert 'http://localhost:11434/v1|bge-m3' in html
    assert 'http://localhost:11434/v1|qwen3-embedding:0.6b' in html
