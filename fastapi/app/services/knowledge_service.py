"""
知识库（Knowledge Base）服务 —— 为智能体提供 RAG 参考资料

支持两类知识库（knowledge_bases.kb_type）：
- local    本地知识库：上传 txt/md/pdf/docx 文档 → 自动切分 → 存 MySQL。
            检索方式（config.retrieval）：
              keyword   关键词检索（2-gram，零外部依赖，默认）
              embedding Embedding 向量检索（OpenAI 兼容 /embeddings，需配置
                        embedding_base_url / embedding_api_key / embedding_model）
- bailian  阿里云百炼云知识库：调用百炼 Retriever API（基于已上传到百炼控制台的知识库）。
            config: base_url / api_key / workspace_id / collection_name
- http     自定义 HTTP 检索接口：POST JSON {"query":..., "top_k":...}，
            config: url / api_key(可选)；响应中递归提取 documents/texts/results/text 列表

智能体通过 agent_knowledge（多对多）关联知识库；回复前检索命中内容注入 prompt（RAG）。
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiomysql
import httpx

logger = logging.getLogger(__name__)

# 阿里云百炼默认地址
DEFAULT_BAILIAN_BASE = "https://dashscope.aliyuncs.com"

# RAG 注入上限（字符），防止 prompt 过长
RAG_MAX_CHARS = 2000
# 单库检索返回片段上限
DEFAULT_TOP_K = 3

# 支持的文档格式
SUPPORTED_EXTS = (".txt", ".md", ".csv", ".json", ".log", ".pdf", ".docx")

RETRIEVE_TIMEOUT = 60


def _bigrams(text: str) -> set:
    """2-gram 字符集（中文友好、零依赖的关键词检索）"""
    s = re.sub(r"\s+", "", text or "").lower()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _split_text(text: str, chunk_size: int, overlap: int) -> List[str]:
    """按字符长度切分（带重叠），简单可靠"""
    text = re.sub(r"\r\n?", "\n", text or "").strip()
    if not text:
        return []
    chunk_size = max(int(chunk_size or 500), 50)
    overlap = min(max(int(overlap or 50), 0), chunk_size // 2)
    step = max(chunk_size - overlap, 1)
    out, start = [], 0
    while start < len(text):
        piece = text[start:start + chunk_size].strip()
        if piece:
            out.append(piece)
        start += step
    return out


def _parse_document(filename: str, data: bytes) -> str:
    """解析文档为纯文本：txt/md 直接读，docx 用标准库 zipfile 提取，pdf 用 pypdf"""
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".txt", ".md", ".csv", ".json", ".log"):
        for enc in ("utf-8", "gb18030"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError("解析 PDF 需要 pypdf，请执行 pip install pypdf") from e
        import io
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if ext == ".docx":
        import io
        import xml.etree.ElementTree as ET
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml_data = z.read("word/document.xml")
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        root = ET.fromstring(xml_data)
        paras = []
        for p in root.findall(".//w:p", ns):
            text = "".join(t.text or "" for t in p.findall(".//w:t", ns))
            if text.strip():
                paras.append(text.strip())
        return "\n".join(paras)
    raise ValueError(f"不支持的文档格式: {ext or '未知'}（支持 {'/'.join(SUPPORTED_EXTS)}）")


async def _embed_texts(cfg: Dict, texts: List[str]) -> List[List[float]]:
    """调用 OpenAI 兼容 /embeddings 批量向量化"""
    base = (cfg.get("embedding_base_url") or "").rstrip("/")
    if not base:
        raise ValueError("本地库 Embedding 检索需先配置 Embedding API 地址")
    url = f"{base}/embeddings"
    headers = {"Content-Type": "application/json"}
    key = cfg.get("embedding_api_key") or ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {"model": cfg.get("embedding_model") or "text-embedding-v3", "input": texts}
    async with httpx.AsyncClient(timeout=RETRIEVE_TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    try:
        return [item["embedding"] for item in data["data"]]
    except (KeyError, TypeError) as e:
        raise RuntimeError(f"Embedding 返回格式异常: {str(data)[:200]}") from e


# ── 内置本地 Embedding（方案 A：零依赖哈希向量）─────────────────
# 无外部模型/API，离线可用。字符 1/2/3-gram 稳定哈希 → 固定维度向量。
# 语义弱于真实模型，但足以区分字面相关的片段（同义词不识别）。

_LOCAL_EMB_DIM = 512


def _stable_hash(s: str) -> int:
    """跨进程稳定的字符串哈希（md5 截断为 64 位 int），不受 PYTHONHASHSEED 影响"""
    return int.from_bytes(hashlib.md5(s.encode("utf-8")).digest()[:8], "big")


def _local_embed(text: str, dim: int = _LOCAL_EMB_DIM) -> List[float]:
    """单个文本 → 归一化哈希向量"""
    s = re.sub(r"\s+", "", (text or "").lower())
    vec = [0.0] * dim
    n = len(s)
    for gram_len in (1, 2, 3):
        for i in range(n - gram_len + 1):
            gram = s[i:i + gram_len]
            h = _stable_hash(gram)
            idx = h % dim
            sign = 1.0 if (h & 1) else -1.0
            vec[idx] += sign
    norm = sum(x * x for x in vec) ** 0.5
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def _local_embed_texts(texts: List[str], dim: int = _LOCAL_EMB_DIM) -> List[List[float]]:
    """批量本地向量化（纯 CPU，零依赖）"""
    return [_local_embed(t, dim) for t in texts]


def _extract_texts_from_response(obj: Any, out: Optional[List[str]] = None) -> List[str]:
    """递归从检索响应中提取文本片段（兼容 documents/texts/results/text 等常见字段）"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("text", "content", "document", "text_content") and isinstance(v, str) and v.strip():
                out.append(v.strip())
            elif k in ("documents", "texts", "results", "records", "data", "chunks", "hits") and isinstance(v, list):
                for item in v:
                    _extract_texts_from_response(item, out)
            elif k == "documents" and isinstance(v, dict):
                _extract_texts_from_response(v, out)
            else:
                _extract_texts_from_response(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _extract_texts_from_response(item, out)
    elif isinstance(obj, str) and obj.strip():
        out.append(obj.strip())
    return out


@dataclass
class KnowledgeBase:
    """知识库信息（dict 便捷包装）"""
    id: int
    name: str
    kb_type: str          # local / bailian / http
    description: str
    config: Dict = field(default_factory=dict)
    chunk_size: int = 500
    chunk_overlap: int = 50
    top_k: int = DEFAULT_TOP_K
    enabled: bool = True
    created_at: str = ""
    doc_count: int = 0
    chunk_count: int = 0


class KnowledgeService:
    """知识库管理服务"""

    def __init__(self):
        self._pool: Optional[aiomysql.Pool] = None

    def set_pool(self, pool: aiomysql.Pool):
        self._pool = pool

    # ── 建表 ────────────────────────────────────────────

    async def ensure_table(self):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_bases (
                        id           INT AUTO_INCREMENT PRIMARY KEY,
                        name         VARCHAR(100) NOT NULL,
                        kb_type      VARCHAR(20)  NOT NULL DEFAULT 'local',
                        description  VARCHAR(500) NOT NULL DEFAULT '',
                        config       TEXT,
                        chunk_size   INT          NOT NULL DEFAULT 500,
                        chunk_overlap INT         NOT NULL DEFAULT 50,
                        top_k        INT          NOT NULL DEFAULT 3,
                        enabled      TINYINT      NOT NULL DEFAULT 1,
                        created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_documents (
                        id          INT AUTO_INCREMENT PRIMARY KEY,
                        kb_id       INT          NOT NULL,
                        filename    VARCHAR(255) NOT NULL,
                        file_path   VARCHAR(500) NOT NULL DEFAULT '',
                        content     MEDIUMTEXT,
                        status      VARCHAR(20)  NOT NULL DEFAULT 'ok',
                        chunk_count INT          NOT NULL DEFAULT 0,
                        created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        KEY idx_kb (kb_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_chunks (
                        id          INT AUTO_INCREMENT PRIMARY KEY,
                        kb_id       INT NOT NULL,
                        doc_id      INT NOT NULL,
                        chunk_index INT NOT NULL DEFAULT 0,
                        content     TEXT,
                        embedding   JSON NULL,
                        UNIQUE KEY uk_chunk (kb_id, doc_id, chunk_index),
                        KEY idx_kb (kb_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_knowledge (
                        agent_id INT NOT NULL,
                        kb_id    INT NOT NULL,
                        PRIMARY KEY (agent_id, kb_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                await conn.commit()
        logger.info("✅ knowledge tables ensured")

    # ── 行 → 对象 ───────────────────────────────────────

    def _row_to_kb(self, row) -> KnowledgeBase:
        cfg = {}
        if row[4]:
            try:
                cfg = json.loads(row[4])
                if not isinstance(cfg, dict):
                    cfg = {}
            except Exception:
                cfg = {}
        return KnowledgeBase(
            id=row[0],
            name=row[1],
            kb_type=row[2],
            description=row[3] or "",
            config=cfg,
            chunk_size=row[5] or 500,
            chunk_overlap=row[6] or 50,
            top_k=row[7] or DEFAULT_TOP_K,
            enabled=bool(row[8]),
            created_at=row[9].isoformat() if row[9] else "",
            doc_count=row[10] if len(row) > 10 and row[10] else 0,
            chunk_count=row[11] if len(row) > 11 and row[11] else 0,
        )

    # ── CRUD ────────────────────────────────────────────

    async def list_kbs(self, enabled_only: bool = False) -> List[KnowledgeBase]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                sql = """
                    SELECT b.id,b.name,b.kb_type,b.description,b.config,
                           b.chunk_size,b.chunk_overlap,b.top_k,b.enabled,b.created_at,
                           (SELECT COUNT(*) FROM knowledge_documents d WHERE d.kb_id=b.id),
                           (SELECT COUNT(*) FROM knowledge_chunks c WHERE c.kb_id=b.id)
                    FROM knowledge_bases b
                """
                if enabled_only:
                    sql += " WHERE b.enabled=1"
                sql += " ORDER BY b.id ASC"
                await cur.execute(sql)
                rows = await cur.fetchall()
        return [self._row_to_kb(r) for r in rows]

    async def get_kb(self, kb_id: int) -> Optional[KnowledgeBase]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT b.id,b.name,b.kb_type,b.description,b.config,
                              b.chunk_size,b.chunk_overlap,b.top_k,b.enabled,b.created_at,
                              (SELECT COUNT(*) FROM knowledge_documents d WHERE d.kb_id=b.id),
                              (SELECT COUNT(*) FROM knowledge_chunks c WHERE c.kb_id=b.id)
                       FROM knowledge_bases b WHERE b.id=%s""",
                    (kb_id,),
                )
                row = await cur.fetchone()
        if row:
            return self._row_to_kb(row)
        return None

    async def create_kb(
        self,
        name: str,
        kb_type: str,
        description: str,
        config: Dict,
        chunk_size: int,
        chunk_overlap: int,
        top_k: int,
    ) -> int:
        name = (name or "").strip()
        if not name:
            raise ValueError("知识库名称不能为空")
        if kb_type not in ("local", "bailian", "http"):
            raise ValueError("知识库类型无效")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT id FROM knowledge_bases WHERE name=%s", (name,))
                if await cur.fetchone():
                    raise ValueError(f"知识库「{name}」已存在")
                await cur.execute(
                    """INSERT INTO knowledge_bases (name,kb_type,description,config,chunk_size,chunk_overlap,top_k,enabled)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,1)""",
                    (name, kb_type, description or "", json.dumps(config, ensure_ascii=False),
                     chunk_size, chunk_overlap, top_k),
                )
                await conn.commit()
                new_id = cur.lastrowid
        logger.info(f"✅ Created knowledge base #{new_id} ({kb_type}) {name}")
        return new_id

    async def update_kb(
        self,
        kb_id: int,
        name: str,
        kb_type: str,
        description: str,
        config: Dict,
        chunk_size: int,
        chunk_overlap: int,
        top_k: int,
        enabled: bool,
    ) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT id FROM knowledge_bases WHERE id=%s", (kb_id,))
                if not await cur.fetchone():
                    return False
                await cur.execute(
                    """UPDATE knowledge_bases
                       SET name=%s, kb_type=%s, description=%s, config=%s,
                           chunk_size=%s, chunk_overlap=%s, top_k=%s, enabled=%s
                       WHERE id=%s""",
                    (name, kb_type, description or "", json.dumps(config, ensure_ascii=False),
                     chunk_size, chunk_overlap, top_k, 1 if enabled else 0, kb_id),
                )
                await conn.commit()
        # 不能以 rowcount 判断成败——字段值与原值相同时 MySQL 返回受影响行数 0
        # （如编辑知识库时只改授权/勾选而基本字段未变）。存在性已在上方检查。
        return True

    async def delete_kb(self, kb_id: int) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM knowledge_chunks WHERE kb_id=%s", (kb_id,))
                await cur.execute("DELETE FROM knowledge_documents WHERE kb_id=%s", (kb_id,))
                await cur.execute("DELETE FROM agent_knowledge WHERE kb_id=%s", (kb_id,))
                await cur.execute(
                    "DELETE FROM knowledge_group_members WHERE kb_id=%s", (kb_id,)
                )
                await cur.execute(
                    "DELETE FROM org_group_members "
                    "WHERE member_type='knowledge' AND member_id=%s",
                    (kb_id,),
                )
                await cur.execute("DELETE FROM knowledge_bases WHERE id=%s", (kb_id,))
                await conn.commit()
                affected = cur.rowcount
        if affected:
            logger.info(f"🗑 Deleted knowledge base #{kb_id}")
            return True
        return False

    # ── 智能体关联 ──────────────────────────────────────

    async def set_agent_kbs(self, agent_id: int, kb_ids: List[int]):
        kb_ids = [int(x) for x in (kb_ids or []) if str(x).strip()]
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM agent_knowledge WHERE agent_id=%s", (agent_id,))
                for kb_id in kb_ids:
                    await cur.execute(
                        "INSERT IGNORE INTO agent_knowledge (agent_id, kb_id) VALUES (%s,%s)",
                        (agent_id, kb_id),
                    )
                await conn.commit()

    async def get_agent_kbs(self, agent_id: int) -> List[int]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT kb_id FROM agent_knowledge WHERE agent_id=%s", (agent_id,)
                )
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def get_kb_agent_count(self, kb_id: int) -> int:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM agent_knowledge WHERE kb_id=%s", (kb_id,)
                )
                row = await cur.fetchone()
        return row[0] if row else 0

    # ── 本地库文档管理 ──────────────────────────────────

    async def list_documents(self, kb_id: int) -> List[Dict]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT id, filename, status, chunk_count, created_at
                       FROM knowledge_documents WHERE kb_id=%s ORDER BY id DESC""",
                    (kb_id,),
                )
                rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "filename": r[1],
                "status": r[2],
                "chunk_count": r[3],
                "created_at": r[4].isoformat() if r[4] else "",
            }
            for r in rows
        ]

    async def upload_document(self, kb_id: int, filename: str, data: bytes) -> int:
        """解析、切分、入库（Embedding 模式同步向量化）。返回 chunk 数。"""
        kb = await self.get_kb(kb_id)
        if not kb:
            raise ValueError("知识库不存在")
        if kb.kb_type != "local":
            raise ValueError("仅本地知识库支持上传文档")
        filename = os.path.basename(filename or "untitled")
        content = await asyncio.to_thread(_parse_document, filename, data)
        if not content.strip():
            raise ValueError("文档内容为空，未解析到文本")
        chunks = _split_text(content, kb.chunk_size, kb.chunk_overlap)
        cfg = kb.config or {}
        retrieval = cfg.get("retrieval") or "keyword"
        use_embedding = retrieval in ("embedding", "local")

        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO knowledge_documents (kb_id, filename, content, status, chunk_count)
                       VALUES (%s,%s,%s,'ok',%s)""",
                    (kb_id, filename, content[:1000000], len(chunks)),
                )
                doc_id = cur.lastrowid
                if not chunks:
                    await cur.execute("UPDATE knowledge_documents SET chunk_count=0 WHERE id=%s", (doc_id,))
                else:
                    for i, c in enumerate(chunks):
                        await cur.execute(
                            """INSERT INTO knowledge_chunks (kb_id, doc_id, chunk_index, content, embedding)
                               VALUES (%s,%s,%s,%s,NULL)""",
                            (kb_id, doc_id, i, c[:8000]),
                        )
                await conn.commit()

        # Embedding 模式：批量向量化后回填（本地哈希向量 / 远程 API）
        if use_embedding and chunks:
            try:
                if retrieval == "local":
                    embs = await asyncio.to_thread(_local_embed_texts, chunks[:256])
                else:
                    embs = await _embed_texts(cfg, chunks[:256])
                async with self._pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        for i, emb in enumerate(embs):
                            await cur.execute(
                                "UPDATE knowledge_chunks SET embedding=%s WHERE kb_id=%s AND doc_id=%s AND chunk_index=%s",
                                (json.dumps(emb), kb_id, doc_id, i),
                            )
                        await conn.commit()
            except Exception as e:
                logger.warning(
                    f"文档 {filename} 向量化失败（将退化为关键词检索）: {e}"
                )
        logger.info(f"📄 Uploaded doc #{doc_id} {filename} -> {len(chunks)} chunks (kb=#{kb_id})")
        return len(chunks)

    async def delete_document(self, doc_id: int) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT kb_id FROM knowledge_documents WHERE id=%s", (doc_id,))
                row = await cur.fetchone()
                if not row:
                    return False
                await cur.execute("DELETE FROM knowledge_chunks WHERE doc_id=%s", (doc_id,))
                await cur.execute("DELETE FROM knowledge_documents WHERE id=%s", (doc_id,))
                await conn.commit()
        return True

    # ── 检索 ────────────────────────────────────────────

    async def retrieve(self, kb: KnowledgeBase, query: str, top_k: Optional[int] = None) -> List[str]:
        """统一检索入口，返回命中的文本片段列表"""
        query = (query or "").strip()
        if not query:
            return []
        top_k = top_k or kb.top_k or DEFAULT_TOP_K
        if kb.kb_type == "local":
            return await self._retrieve_local(kb, query, top_k)
        if kb.kb_type == "bailian":
            return await self._retrieve_bailian(kb, query, top_k)
        if kb.kb_type == "http":
            return await self._retrieve_http(kb, query, top_k)
        return []

    async def _retrieve_local(self, kb: KnowledgeBase, query: str, top_k: int) -> List[str]:
        cfg = kb.config or {}
        retrieval = cfg.get("retrieval") or "keyword"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                if retrieval in ("embedding", "local"):
                    try:
                        if retrieval == "local":
                            q_emb = (await asyncio.to_thread(_local_embed_texts, [query]))[0]
                        else:
                            q_emb = (await _embed_texts(cfg, [query]))[0]
                        await cur.execute(
                            "SELECT content, embedding FROM knowledge_chunks WHERE kb_id=%s AND embedding IS NOT NULL",
                            (kb.id,),
                        )
                        rows = await cur.fetchall()
                        scored = []
                        for content, emb_json in rows:
                            try:
                                emb = json.loads(emb_json)
                            except Exception:
                                continue
                            score = _cosine(q_emb, emb)
                            if content:
                                scored.append((score, content))
                        scored.sort(key=lambda x: x[0], reverse=True)
                        return [c for _, c in scored[:top_k] if _ > 0.2]
                    except Exception as e:
                        logger.warning(f"Embedding 检索失败，退化关键词检索 kb=#{kb.id}: {e}")
                # 关键词（2-gram Jaccard + 覆盖率）
                await cur.execute(
                    "SELECT content FROM knowledge_chunks WHERE kb_id=%s", (kb.id,)
                )
                rows = await cur.fetchall()
        q_grams = _bigrams(query)
        scored = []
        for (content,) in rows:
            if not content:
                continue
            grams = _bigrams(content)
            jac = _jaccard(q_grams, grams)
            cover = len(q_grams & grams) / float(len(q_grams)) if q_grams else 0.0
            score = 0.5 * jac + 0.5 * cover
            if score > 0:
                scored.append((score, content))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:top_k]]

    async def _retrieve_bailian(self, kb: KnowledgeBase, query: str, top_k: int) -> List[str]:
        cfg = kb.config or {}
        base = (cfg.get("base_url") or DEFAULT_BAILIAN_BASE).rstrip("/")
        api_key = cfg.get("api_key") or ""
        if not api_key:
            raise ValueError("百炼知识库未配置 api_key")
        url = f"{base}/api/v1/retriever/retrieve"
        payload: Dict[str, Any] = {"query": query, "top_k": top_k}
        if cfg.get("workspace_id"):
            payload["workspace_id"] = cfg["workspace_id"]
        if cfg.get("collection_name"):
            payload["collection_name"] = cfg["collection_name"]
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=RETRIEVE_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"百炼检索失败 HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
        texts = _extract_texts_from_response(data)
        return texts[:top_k]

    async def _retrieve_http(self, kb: KnowledgeBase, query: str, top_k: int) -> List[str]:
        cfg = kb.config or {}
        url = (cfg.get("url") or "").strip()
        if not url:
            raise ValueError("HTTP 知识库未配置检索地址 url")
        headers = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        async with httpx.AsyncClient(timeout=RETRIEVE_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json={"query": query, "top_k": top_k})
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP 检索失败 HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
        texts = _extract_texts_from_response(data)
        return texts[:top_k]

    # ── RAG 组装 ────────────────────────────────────────

    async def build_rag_context(self, agent_id: int, query: str, max_chars: int = RAG_MAX_CHARS) -> str:
        """检索智能体关联的全部知识库，拼装为可注入 prompt 的参考资料文本。无命中返回空串。"""
        query = (query or "").strip()
        if not query:
            return ""
        kb_ids = await self.get_agent_kbs(agent_id)
        if not kb_ids:
            return ""
        kbs = [kb for kb in await self.list_kbs(enabled_only=True) if kb.id in kb_ids]
        if not kbs:
            return ""
        parts: List[str] = []
        total = 0
        for kb in kbs:
            if total >= max_chars:
                break
            try:
                hits = await self.retrieve(kb, query, top_k=kb.top_k or DEFAULT_TOP_K)
            except Exception as e:
                logger.warning(f"知识库 #{kb.id} 检索失败: {e}")
                continue
            for h in hits:
                h = (h or "").strip()
                if not h:
                    continue
                remain = max_chars - total
                if remain <= 0:
                    break
                piece = f"[{kb.name}] {h}"[:remain]
                parts.append(piece)
                total += len(piece)
        return "\n".join(parts)


knowledge_service = KnowledgeService()
