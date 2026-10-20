"""记忆集清理：让智能体把「智能体自己写的」客户事实压缩成更少、不重复的"当前状态"。

三条边界（**代码强制，不靠提示词** —— 提示词只是让模型知道有这些事实）：
  ① 只处理"自上次清理以来**有新的智能体事实**"的客户（`dirty_customers`），新记忆很少时
     不会把整套记忆反复丢给 LLM；
  ② 只删改 `source='agent'` 的条目；`source='manual'`（人工补录 / 导入）**一个都不动** ——
     即使模型把它们写进整理结果，服务端也会在落库前丢弃（见 `_apply`）；
  ③ 模型返回空 / JSON 解析失败 / 调用异常 → **什么都不删**，只写一条失败日志。

每次清理都往 `memory_cleanup_log` 留档（清理前/后的清单），因为清理是唯一会删记忆的操作。
"""

import json
import logging
import re
from typing import Dict, List, Optional, Tuple

from . import memory_service
from .memory_service import (
    CLEANUP_MAX_CUSTOMERS_PER_RUN,
    CLEANUP_TICK_SECONDS,
    ITEM_MAX_CHARS,
    content_md5,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是"客户记忆整理员"。任务：把某个客户的记忆压缩成更少、互不重复的**当前状态**事实。

规则：
1. 只整理【智能体记录的事实】；【人工维护的事实】是只读参考，**不要**把它们写进结果，也不要与它们冲突。
   当客户是「通用记忆」时（没有具体客户），这些是公司层面的统一规则/话术 —— 同样要**保留**（可合并同类项），
   不要因为"它不是某个客户的事实"就返回空数组。
2. 保留仍然有效的信息：客户身份/公司/角色、偏好与禁忌、产品兴趣、订购意向、订单要点、承诺与约定时间、投诉与顾虑。
3. 被后续事实取代的旧事实要合并掉（例：「账期改成 40 天」「账期改成 45 天」「账期 35 天」→ 合并为「账期要求 35 天（此前提过 40/45 天）」）。
4. 每条一句话，只写客户的事实，不写评价与推测；单条不超过 %d 字；不要把寒暄当成事实。
5. 结果**只输出一个 JSON 数组**（字符串数组），不要任何解释、不要 markdown 代码块。
   确实没有任何值得保留的事实就输出空数组 []。

输出示例：
["客户是苏州的经销商", "账期要求 35 天（此前提过 40/45 天）"]""" % ITEM_MAX_CHARS


def _extract_json_array(text: str) -> Optional[List[str]]:
    """从模型输出里抠出 JSON 数组（容忍 ```json 围栏和前后废话）。失败返回 None。"""
    if not text:
        return None
    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    out: List[str] = []
    for x in data:
        if isinstance(x, str) and x.strip():
            out.append(x.strip())
    return out


class MemoryCleanupService:
    def __init__(self):
        self._task = None
        self._running = False

    # ── 调度 ────────────────────────────────────────────

    async def start(self):
        import asyncio

        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.get_event_loop().create_task(self._loop())
        logger.info(f"🧹 记忆清理调度已启动（每 {CLEANUP_TICK_SECONDS // 60} 分钟检查一次）")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self):
        import asyncio

        await asyncio.sleep(30)   # 让启动流程先跑完
        while self._running:
            try:
                for st in await memory_service.due_cleanup_sets():
                    await self.cleanup_set(st["id"], trigger="auto")
            except Exception:
                logger.exception("记忆清理调度出错（不影响服务）")
            await asyncio.sleep(CLEANUP_TICK_SECONDS)

    # ── 一次清理 ────────────────────────────────────────

    async def cleanup_set(self, set_id: int, trigger: str = "manual") -> Dict:
        """清理一套记忆：只处理有新增智能体事实的客户。返回汇总（给页面/日志用）。"""
        st = await memory_service.get_set(set_id)
        if not st:
            return {"ok": False, "note": "记忆集不存在", "customers": 0, "before": 0, "after": 0}
        agent_id = int(st.get("cleanup_agent_id") or 0)
        if not agent_id:
            return {"ok": False, "note": "未指定清理用的智能体", "customers": 0, "before": 0, "after": 0}

        from .agent_service import agent_service

        agent = await agent_service.get_agent(agent_id)
        if agent is None:
            return {"ok": False, "note": f"智能体 #{agent_id} 不存在", "customers": 0, "before": 0, "after": 0}

        dirty = await memory_service.dirty_customers(set_id)
        summary = {"ok": True, "customers": len(dirty), "before": 0, "after": 0,
                   "skipped": 0, "failed": 0, "note": f"{trigger}·{len(dirty)} 位客户"}
        if not dirty:
            await memory_service.mark_cleaned(set_id)
            return summary

        for c in dirty:
            try:
                before, after, status, note = await self._cleanup_customer(
                    set_id, agent, c["customer_id"], c["name"]
                )
            except Exception as e:
                logger.exception(f"清理客户失败 set={set_id} customer={c['customer_id']}")
                before, after, status, note = [], [], "failed", f"异常: {e}"[:200]
            summary["before"] += len(before)
            summary["after"] += len(after)
            if status == "ok":
                pass
            elif status == "skipped":
                summary["skipped"] += 1
            else:
                summary["failed"] += 1
            await memory_service.log_cleanup(
                set_id, c["customer_id"], c["name"], agent_id, status, before, after, note
            )
        await memory_service.mark_cleaned(set_id)
        logger.info(f"🧹 清理完成 set={set_id} {summary}")
        return summary

    async def _cleanup_customer(
        self, set_id: int, agent, customer_id: int, customer_name: str
    ) -> Tuple[List[str], List[str], str, str]:
        """整理一位客户。返回 (被删除的旧条目, 新写入的条目, 状态, 说明)。

    没删任何东西的分支（解析失败/模型返回空）一律返回空列表 —— 日志里的"条目变化"
    必须如实反映**真的删了什么**，否则 2 → 0 看起来像被清空了。
    """
        from .agent_service import agent_service

        agent_items = await memory_service.items_by_source(set_id, customer_id, "agent")
        manual_items = await memory_service.items_by_source(set_id, customer_id, "manual")
        if not agent_items:
            return [], [], "skipped", "没有智能体写入的事实"

        who = customer_name or ("通用记忆" if not customer_id else f"客户#{customer_id}")
        lines_agent = "\n".join(f"{i+1}. {it['content']}" for i, it in enumerate(agent_items))
        manual_block = (
            "\n".join(f"- {it['content']}" for it in manual_items)
            or "（无）"
        )
        user_prompt = (
            f"客户：{who}\n\n【智能体记录的事实】（要整理的就是这些）\n{lines_agent}\n\n"
            f"【人工维护的事实】（只读参考，不要写进结果，也不要与它们冲突）\n{manual_block}"
        )
        text = await agent_service._chat_completion(
            agent, [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt}],
            allow_extra_tools=False,
        )
        parsed = _extract_json_array(text)
        if parsed is None:
            logger.warning(f"清理输出无法解析 set={set_id} customer={customer_id}: {str(text)[:120]}")
            return [], [], "failed", f"模型输出无法解析为 JSON 数组（原 {len(agent_items)} 条未改动）"
        if agent_items and not parsed:
            # 有事实却要清空 → 大概率是模型抽风，宁可不清（删掉就找不回来了）
            logger.warning(f"清理输出为空但原有 {len(agent_items)} 条 set={set_id} customer={customer_id}")
            return [], [], "skipped", f"模型返回空数组，已跳过（原 {len(agent_items)} 条未改动）"

        # 与人工事实去重（模型可能把只读参考又写回来）+ 去重 + 截断
        manual_md5 = {content_md5(i["content"]) for i in manual_items}
        kept: List[str] = []
        seen = set()
        dropped_manual = 0
        for content in parsed:
            md5 = content_md5(content)
            if md5 in manual_md5:
                dropped_manual += 1
                continue
            if md5 in seen:
                continue
            seen.add(md5)
            kept.append(content[:ITEM_MAX_CHARS])
        if not kept and agent_items:
            return [], [], "skipped", f"整理结果为空，已跳过（原 {len(agent_items)} 条未改动）"

        old, written = await memory_service.replace_agent_items(set_id, customer_id, kept)
        note = f"{len(old)} → {len(written)} 条"
        if dropped_manual:
            note += f"（丢弃了 {dropped_manual} 条与人工事实重复的内容）"
        return old, written, "ok", note


memory_cleanup_service = MemoryCleanupService()
