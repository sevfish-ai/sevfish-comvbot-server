#!/usr/bin/env python3
"""一次性迁移：把「顺序下一个」这条隐式边物化成显式跳转。

背景
----
原来 `next_on_success` 留空 = 「顺序上的下一个节点」，于是**节点的位置会悄悄改变运行
行为**（追加节点把两条分支串起来、合并点位置不对就判循环……）。改成纯显式图模型后，
留空 = 到此结束。

为了让你现有的工作流在迁移前后**运行结果完全一致**，必须先把每条隐式边按当前顺序
原样写成显式的 `next_on_success`：

    第 i 个节点（next 为空且不是 switch） → 第 i+1 个节点的名字
    最后一个节点（next 为空且不是 switch） → __end__（到此结束）
    switch 节点 → 跳过（它用 branches / default_next，不用 next_on_success）
    已经有显式跳转的 → 不动

默认干跑（只打印将要写的值），确认无误后加 --apply 落库。

运行：
    venv/bin/python fastapi/migrate_explicit_edges.py           # 干跑
    venv/bin/python fastapi/migrate_explicit_edges.py --apply   # 落库
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiomysql

from app.config import settings
from app.models.workflow import END_TARGET


async def main(apply: bool) -> None:
    pool = await aiomysql.create_pool(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_DATABASE,
        autocommit=True,
        minsize=1,
        maxsize=3,
    )
    planned = applied = 0
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT id, name FROM command_workflows ORDER BY id")
                workflows = await cur.fetchall()
                if not workflows:
                    print("没有工作流，无需迁移")
                    return
                for wid, wname in workflows:
                    await cur.execute(
                        """SELECT id, name, node_type, sort, next_on_success
                           FROM workflow_nodes WHERE workflow_id=%s
                           ORDER BY sort ASC, id ASC""",
                        (wid,),
                    )
                    nodes = await cur.fetchall()
                    print(f"\n工作流 #{wid}「{wname}」（{len(nodes)} 个节点）")
                    known = {n[1] for n in nodes}
                    for i, (nid, name, ntype, _sort, nxt) in enumerate(nodes):
                        nxt = (nxt or "").strip()
                        if ntype == "switch":
                            print(f"  [{i + 1}] {name:<14} switch，用分支定义 → 跳过")
                            continue
                        if nxt:
                            flag = "" if (nxt == END_TARGET or nxt in known) else "  ⚠ 目标不存在"
                            print(f"  [{i + 1}] {name:<14} 已有跳转 → {nxt}{flag}")
                            continue
                        target = nodes[i + 1][1] if i + 1 < len(nodes) else END_TARGET
                        print(f"  [{i + 1}] {name:<14} 空（顺序下一个）→ 物化为 {target}")
                        planned += 1
                        if apply:
                            await cur.execute(
                                "UPDATE workflow_nodes SET next_on_success=%s WHERE id=%s",
                                (target, nid),
                            )
                            applied += 1
    finally:
        pool.close()
        await pool.wait_closed()

    print()
    if apply:
        print(f"✅ 已物化 {applied} 条隐式跳转")
    else:
        print(f"（干跑）将物化 {planned} 条隐式跳转；确认无误后加 --apply 落库")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
