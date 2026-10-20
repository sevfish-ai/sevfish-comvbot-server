"""知识库向量回填/重算（手工脚本，pytest 不收集；**会调 Embedding API**）

为什么需要：`knowledge_chunks.embedding` 只在上传文档时按**当时**的知识库配置写入。
把知识库从"关键词检索"改成"Embedding 向量检索"（阶段 D1）之后，存量 chunk 的 embedding 仍是 NULL，
而 `_retrieve_local` 只筛 `embedding IS NOT NULL`（`knowledge_service.py:583`）——向量检索一条都取不到，
**不报错、日志干净、结果全空**。换过模型（比如从 512 维哈希向量换到 1024 维 bge-m3）更要重算：
`_cosine` 的 `sum(x*y for x,y in zip(a,b))`（`knowledge_service.py:58`）会静默截断到较短的那个，混存＝结果全错。

本脚本只写 `knowledge_chunks.embedding` 一列，不删任何数据；走的是与上传完全相同的
`_embed_texts` / `_local_embed_texts`，不复制第二套向量化实现。

用法：
    cd fastapi && ../venv/bin/python tests/manual_reembed.py                    # 只补 embedding 为空的
    ../venv/bin/python tests/manual_reembed.py --kb 1125                        # 只处理某个知识库（可重复）
    ../venv/bin/python tests/manual_reembed.py --force                          # 全部重算（换模型后用这个）
    ../venv/bin/python tests/manual_reembed.py --dry-run --batch 16             # 只看会处理多少条
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiomysql  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.knowledge_service import (  # noqa: E402
    _embed_texts,
    _local_embed_texts,
    knowledge_service,
)


async def reembed_kb(pool, kb, force: bool, batch: int, dry: bool) -> dict:
    cfg = kb.config or {}
    retrieval = cfg.get("retrieval") or "keyword"
    if retrieval not in ("embedding", "local"):
        print(f"· 知识库 #{kb.id} {kb.name}：检索方式={retrieval}，不需要向量，跳过")
        return {"kb": kb.id, "name": kb.name, "skip": retrieval}

    where = "kb_id=%s" if force else "kb_id=%s AND embedding IS NULL"
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT id, content FROM knowledge_chunks WHERE {where} ORDER BY id", (kb.id,)
            )
            rows = await cur.fetchall()
    cfg_desc = cfg.get("embedding_model") or retrieval
    print(f"· 知识库 #{kb.id} {kb.name}：待处理 {len(rows)} 条（{'全部重算' if force else '仅空向量'}，{cfg_desc}）")
    if not rows:
        return {"kb": kb.id, "name": kb.name, "done": 0, "failed": 0, "dims": []}

    done, failed, dims = 0, 0, set()
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        texts = [(r[1] or "")[:8000] for r in part]
        try:
            embs = (
                await asyncio.to_thread(_local_embed_texts, texts)
                if retrieval == "local"
                else await _embed_texts(cfg, texts)
            )
        except Exception as e:
            print(f"    ❌ 第 {i // batch + 1} 批失败（{len(part)} 条）：{e}")
            failed += len(part)
            continue
        if len(embs) != len(part):
            print(f"    ❌ 第 {i // batch + 1} 批返回 {len(embs)} 条，期望 {len(part)} 条")
            failed += len(part)
            continue
        for _, emb in zip(part, embs):
            dims.add(len(emb))
        if not dry:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    for (cid, _), emb in zip(part, embs):
                        await cur.execute(
                            "UPDATE knowledge_chunks SET embedding=%s WHERE id=%s",
                            (json.dumps(emb), cid),
                        )
        done += len(part)
    print(f"    → 已{'试算' if dry else '写入'} {done} 条，失败 {failed} 条，维度 {sorted(dims)}")
    return {"kb": kb.id, "name": kb.name, "done": done, "failed": failed, "dims": sorted(dims)}


async def main():
    ap = argparse.ArgumentParser(description="知识库向量回填/重算")
    ap.add_argument("--kb", type=int, action="append", help="只处理指定知识库 id（可重复）")
    ap.add_argument("--force", action="store_true", help="已有向量的 chunk 也重算（换模型后用）")
    ap.add_argument("--batch", type=int, default=32, help="每批条数（默认 32）")
    ap.add_argument("--dry-run", action="store_true", help="只向量化不写库")
    args = ap.parse_args()

    pool = await aiomysql.create_pool(
        host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD, db=settings.MYSQL_DATABASE,
        charset="utf8mb4", autocommit=True,
    )
    knowledge_service.set_pool(pool)
    try:
        kbs = (
            [await knowledge_service.get_kb(k) for k in args.kb]
            if args.kb
            else await knowledge_service.list_kbs()
        )
        kbs = [kb for kb in kbs if kb]
        if not kbs:
            print("没有可处理的知识库")
            return 1
        results = [await reembed_kb(pool, kb, args.force, args.batch, args.dry_run) for kb in kbs]

        # 收尾核对：维度必须一致（混维度＝_cosine 静默截断，结果全错）
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT kb_id,
                              SUM(embedding IS NOT NULL) AS embedded,
                              COUNT(*) AS total,
                              MIN(JSON_LENGTH(embedding)) AS min_dim,
                              MAX(JSON_LENGTH(embedding)) AS max_dim
                       FROM knowledge_chunks GROUP BY kb_id ORDER BY kb_id"""
                )
                print("\n维度核对（min_dim 必须等于 max_dim）：")
                ok = True
                for kb_id, embedded, total, min_dim, max_dim in await cur.fetchall():
                    bad = min_dim is not None and min_dim != max_dim
                    ok = ok and not bad
                    print(f"  知识库 #{kb_id}: {embedded}/{total} 条有向量，维度 {min_dim}~{max_dim}"
                          f"{'  ⚠️ 维度不一致' if bad else ''}")
        failed = sum(r.get("failed", 0) for r in results)
        if failed:
            print(f"\n❌ 有 {failed} 条失败，重跑本脚本可续传（只补空向量）")
            return 1
        print("\n✅ 完成" if ok else "\n⚠️ 完成，但存在维度不一致，请检查是否混用了不同模型")
        return 0
    finally:
        pool.close()
        await pool.wait_closed()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
