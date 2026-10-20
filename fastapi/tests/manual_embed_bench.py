"""Embedding 性能基准（手工脚本，pytest 不收集；**会调 Embedding API**）

为什么需要：E3 是 CPU 推理（无 GPU），而 embedding 在两条链路里都是**同步等待**——
上传文档时逐批向量化，以及**每次检索都要先把查询语句向量化一次**（`_embed_texts`，
`knowledge_service.py:119`）。后者直接决定客户消息的响应延迟，所以要在真机上量清楚：
冷启动、热态单条延迟、批量吞吐、返回维度，以及 `OLLAMA_NUM_PARALLEL=1` 时的并发行为。

走的是生产同一条 `_embed_texts`，不复制第二套调用实现。

用法：
    cd fastapi && ../venv/bin/python tests/manual_embed_bench.py               # 默认 ollama + bge-m3
    ../venv/bin/python tests/manual_embed_bench.py --kb 1125                  # 用真实知识库的配置（最贴近生产）
    ../venv/bin/python tests/manual_embed_bench.py --rounds 20 --batch 1,8,32
    ../venv/bin/python tests/manual_embed_bench.py --concurrent 4             # 看并发是排队还是真并行
"""

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiomysql  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.knowledge_service import (  # noqa: E402
    _embed_texts,
    knowledge_service,
)

SAMPLE = (
    "企业资料：知识库回答「我们卖什么」，长期记忆回答「这位客户要什么」。"
    "AI 员工以企业提供的资料为准，客户事实按人归档，下次对话自动带上。"
)


def text_of(chars: int) -> str:
    return (SAMPLE * (chars // len(SAMPLE) + 1))[:chars]


def ms(seconds: float) -> str:
    return f"{seconds * 1000:.1f}"


def pct(vals, q: float) -> float:
    vals = sorted(vals)
    if not vals:
        return 0.0
    return vals[min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))]


async def timed(cfg, texts):
    t0 = time.perf_counter()
    embs = await _embed_texts(cfg, texts)
    return time.perf_counter() - t0, embs


async def cfg_from_kb(kb_id: int) -> dict:
    pool = await aiomysql.create_pool(
        host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD, db=settings.MYSQL_DATABASE,
        charset="utf8mb4", autocommit=True,
    )
    knowledge_service.set_pool(pool)
    try:
        kb = await knowledge_service.get_kb(kb_id)
        if not kb:
            raise SystemExit(f"知识库 #{kb_id} 不存在")
        cfg = kb.config or {}
        if not cfg.get("embedding_base_url"):
            raise SystemExit(f"知识库 #{kb_id} 未配置 Embedding 地址（检索方式={cfg.get('retrieval')}）")
        return cfg
    finally:
        pool.close()
        await pool.wait_closed()


async def main() -> int:
    ap = argparse.ArgumentParser(description="Embedding 性能基准")
    ap.add_argument("--kb", type=int, help="从该知识库读 Embedding 配置（最贴近生产）")
    ap.add_argument("--base", default="http://127.0.0.1:11434/v1", help="OpenAI 兼容 base_url")
    ap.add_argument("--model", default="bge-m3")
    ap.add_argument("--key", default="ollama")
    ap.add_argument("--chars", type=int, default=200, help="每段文本字数（默认 200）")
    ap.add_argument("--rounds", type=int, default=10, help="热态单条测量轮数")
    ap.add_argument("--batch", default="1,8,32", help="批量吞吐测试的批大小，逗号分隔")
    ap.add_argument("--concurrent", type=int, default=0, help="并发请求数（0=不测）")
    args = ap.parse_args()

    if args.kb:
        cfg = dict(await cfg_from_kb(args.kb))
        print(f"配置来自知识库 #{args.kb}")
    else:
        cfg = {}
    cfg.setdefault("embedding_base_url", "")
    cfg["embedding_base_url"] = cfg["embedding_base_url"] or args.base
    cfg["embedding_model"] = cfg.get("embedding_model") or args.model
    cfg.setdefault("embedding_api_key", "")
    cfg["embedding_api_key"] = cfg["embedding_api_key"] or args.key

    text = text_of(args.chars)
    print(f"目标：{cfg['embedding_base_url']} · 模型 {cfg['embedding_model']} · 文本 {len(text)} 字")

    try:
        cold, embs = await timed(cfg, [text])
    except Exception as e:
        print(f"❌ 首次调用失败：{type(e).__name__}: {e}")
        print("   → 检查 ollama 在跑（127.0.0.1:11434）、模型已拉（ollama list）、base_url/模型名一致")
        return 1
    dims = {len(e) for e in embs}
    print(f"冷启动（含模型加载）：{ms(cold)} ms，维度 {sorted(dims)}")

    hot = []
    for _ in range(max(1, args.rounds)):
        dt, embs = await timed(cfg, [text])
        hot.append(dt)
        dims |= {len(e) for e in embs}
    print(f"热态单条 × {len(hot)}：min {ms(min(hot))} / p50 {ms(pct(hot, .5))} / "
          f"p95 {ms(pct(hot, .95))} / max {ms(max(hot))} ms")

    print("批量吞吐：")
    for b in [int(x) for x in args.batch.split(",") if x.strip()]:
        try:
            dt, embs = await timed(cfg, [text] * b)
        except Exception as e:
            print(f"  批 {b:<4}：❌ {type(e).__name__}: {str(e)[:100]}")
            continue
        dims |= {len(e) for e in embs}
        print(f"  批 {b:<4}：{b / dt:.1f} 条/秒，每条 {ms(dt / b)} ms（整批 {ms(dt)} ms）")

    if args.concurrent > 1:
        t0 = time.perf_counter()
        await asyncio.gather(*[_embed_texts(cfg, [text]) for _ in range(args.concurrent)])
        wall = time.perf_counter() - t0
        serial = pct(hot, .5) * args.concurrent
        verdict = "排队（并发不提速）" if wall > serial * 0.7 else "真并行"
        print(f"并发 {args.concurrent} 路：墙钟 {ms(wall)} ms，串行约 {ms(serial)} ms → {verdict}")

    p95 = pct(hot, .95)
    if p95 <= 0.3:
        judge = "可接受"
    elif p95 <= 0.8:
        judge = "偏慢，客户会感知到等待"
    else:
        judge = "过慢，考虑换更小的模型或更强的机器"
    print(f"\n维度：{sorted(dims)}" + ("  ✅" if len(dims) == 1 else "  ⚠️ 维度不一致，请检查模型"))
    print(f"判定：检索侧单条 p95 {ms(p95)} ms — {judge}（阈值 300 ms，embedding 是检索链路上的同步等待）")
    return 0 if len(dims) == 1 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
