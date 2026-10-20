"""工作流「在节点下方插入」手工验证（需要真实 MySQL，不进 CI）

验证节点增删的接线语义（纯显式图模型，顺序不参与运行语义）：
- insert_node_after：源节点 → 新节点 → 源节点原来的去向
- 删除节点会清掉指向它的跳转
- 两条分支各挂一个节点时互不串联
- 分支链尾继续加节点后仍然收尾
用完即删（临时工作流，失败也会清理）。

运行：venv/bin/python fastapi/tests/manual_workflow_insert.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiomysql

from app.config import settings
from app.models.workflow import END_TARGET, WorkflowCreateRequest, WorkflowNodeRequest
from app.routes.workflows import _wf_edges, admin_workflow_node_insert
from app.services import workflow_service

TMP_NAME = "__tmp_insert_test"


async def main():
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
    workflow_service.set_pool(pool)
    wid = None
    try:
        wid = await workflow_service.create_workflow(
            WorkflowCreateRequest(name=TMP_NAME, description="insert 自检，可删")
        )
        # 显式接线成一条链：a → b → c → 结束（顺序不参与语义，全靠这些跳转）
        a = await workflow_service.add_node(
            wid,
            WorkflowNodeRequest(node_type="llm", name="a", params={}, next_on_success="b"),
        )
        await workflow_service.add_node(
            wid,
            WorkflowNodeRequest(node_type="llm", name="b", params={}, next_on_success="c"),
        )
        await workflow_service.add_node(
            wid,
            WorkflowNodeRequest(
                node_type="llm", name="c", params={}, next_on_success=END_TARGET
            ),
        )
        mid = await workflow_service.insert_node_after(
            wid, a, WorkflowNodeRequest(node_type="output", name="", params={})
        )
        await workflow_service.insert_node_after(
            wid, mid, WorkflowNodeRequest(node_type="output", name="插2", params={})
        )

        nodes = (await workflow_service.get_workflow(wid))["nodes"]
        by_id = {n["id"]: n for n in nodes}
        by_name = {n["name"]: n for n in nodes}
        mid_name = by_id[mid]["name"]
        print("节点（按 id）:", [n["name"] for n in nodes])
        # 插到 a 后面：a → 新节点 → b；再插一次：新节点 → 插2 → b
        assert by_name["a"]["next_on_success"] == mid_name, by_name["a"]
        assert by_name[mid_name]["next_on_success"] == "插2", by_name[mid_name]
        assert by_name["插2"]["next_on_success"] == "b", by_name["插2"]
        assert by_name["b"]["next_on_success"] == "c", by_name["b"]
        assert by_name["c"]["next_on_success"] == END_TARGET, by_name["c"]
        assert mid_name.startswith("node_"), mid_name  # 留空自动命名
        print("✅ insert_node_after 接线正确（源节点 → 新节点 → 原去向，链条不断）")

        # ── 分支挂节点：点某条分支线上的「＋」应该只改那一条分支 ──
        sw = await workflow_service.add_node(
            wid,
            WorkflowNodeRequest(
                node_type="switch",
                name="sw",
                params={
                    "field": "input.user_text",
                    "branches": [
                        {"label": "含退款", "op": "like", "value": "退款", "next": ""},
                        {"label": "含订单", "op": "like", "value": "订单", "next": ""},
                    ],
                    "default_next": "",
                },
            ),
        )
        await admin_workflow_node_insert(wid, sw, org_id=0, branch=1)
        wf = await workflow_service.get_workflow(wid)
        sw_node = next(n for n in wf["nodes"] if n["id"] == sw)
        b0, b1 = sw_node["params"]["branches"]
        print("分支0 next:", repr(b0["next"]), "| 分支1 next:", repr(b1["next"]))
        assert b0["next"] == "", "另一条分支不该被改动"
        assert b1["next"], "被点的那条分支应该指向新节点"
        # 新节点必须是追加在末尾的（插在 switch 后面会让别的空分支也续接到它身上）
        assert wf["nodes"][-1]["name"] == b1["next"], [n["name"] for n in wf["nodes"]]

        # 兜底（branch=-1）
        await admin_workflow_node_insert(wid, sw, org_id=0, branch=-1)
        wf = await workflow_service.get_workflow(wid)
        sw_node = next(n for n in wf["nodes"] if n["id"] == sw)
        assert sw_node["params"]["default_next"], sw_node["params"]
        assert sw_node["params"]["branches"][0]["next"] == "", "兜底不该动分支"
        print("✅ 分支/兜底「＋」只改被点的那一条线，节点追加在末尾")

        # ── 删除分支节点后必须清掉指向它的跳转（否则分支变死引用，画布上连「＋」都没了）──
        dead_name = sw_node["params"]["branches"][1]["next"]
        dead_id = next(n["id"] for n in wf["nodes"] if n["name"] == dead_name)
        await workflow_service.delete_node(dead_id)
        wf = await workflow_service.get_workflow(wid)
        sw_node = next(n for n in wf["nodes"] if n["id"] == sw)
        assert sw_node["params"]["branches"][1]["next"] == "", sw_node["params"]["branches"][1]
        assert all(n["name"] != dead_name for n in wf["nodes"])
        # 画布上该分支应重新变成"可挂节点"（open）而不是死引用
        canvas_edges = _wf_edges(sorted(wf["nodes"], key=lambda x: x["id"]))
        sw_edges = [e for e in canvas_edges if e["src"] == len(wf["nodes"]) - 1]
        assert all(e["open"] and not e["dead"] for e in sw_edges if e["branch"] is not None), sw_edges
        print("✅ 删除分支节点会清掉引用，分支重新变回可挂节点（不留死引用）")

        # ── 回归：两条向下分支各建一个节点，B 不能因为"列表里挨着 A"被串到 A 下面 ──
        sw2 = await workflow_service.add_node(
            wid,
            WorkflowNodeRequest(
                node_type="switch",
                name="sw2",
                params={
                    "field": "input.user_text",
                    "branches": [
                        {"label": "X", "op": "like", "value": "x", "next": ""},
                        {"label": "Y", "op": "like", "value": "y", "next": ""},
                    ],
                    "default_next": "",
                },
            ),
        )
        await admin_workflow_node_insert(wid, sw2, org_id=0, branch=0)  # → A
        await admin_workflow_node_insert(wid, sw2, org_id=0, branch=1)  # → B
        wf = await workflow_service.get_workflow(wid)
        sw2_node = next(n for n in wf["nodes"] if n["id"] == sw2)
        name_a = sw2_node["params"]["branches"][0]["next"]
        name_b = sw2_node["params"]["branches"][1]["next"]
        node_a = next(n for n in wf["nodes"] if n["name"] == name_a)
        node_b = next(n for n in wf["nodes"] if n["name"] == name_b)
        print(f"分支X → {name_a} (next={node_a['next_on_success']!r})")
        print(f"分支Y → {name_b} (next={node_b['next_on_success']!r})")
        assert name_a != name_b
        assert node_a["next_on_success"] == END_TARGET, "A 必须收尾，否则会流向后面追加的 B"
        assert node_b["next_on_success"] == END_TARGET
        edges2 = _wf_edges(sorted(wf["nodes"], key=lambda x: x["id"]))
        assert not any(
            e["src"] == wf["nodes"].index(node_a) and e["dst"] is not None for e in edges2
        ), "A 不该有任何后续边"
        print("✅ 两条分支各挂一个节点，互不串联（A 收尾，不再流向 B）")

        # ── 在 A 下面继续加节点：A → M，M 保持收尾 ──
        m_id = await workflow_service.insert_node_after(
            wid, node_a["id"], WorkflowNodeRequest(node_type="output", name="M", params={})
        )
        wf = await workflow_service.get_workflow(wid)
        node_a = next(n for n in wf["nodes"] if n["name"] == name_a)
        node_m = next(n for n in wf["nodes"] if n["id"] == m_id)
        assert node_a["next_on_success"] == "M", node_a["next_on_success"]
        assert node_m["next_on_success"] == END_TARGET, node_m["next_on_success"]
        print("✅ 在分支节点下面继续加节点：A → M，M 仍然收尾（不会被别的分支节点串走）")

        # ── 起点（id 最小）不能删：删了整条流程的入口就悄悄换人了 ──
        wf = await workflow_service.get_workflow(wid)
        start = min(wf["nodes"], key=lambda n: n["id"])
        try:
            await workflow_service.delete_node(start["id"])
            raise AssertionError(f"起点 {start['name']} 竟然被删掉了")
        except ValueError as e:
            print("  起点删除被拒绝:", e)
        wf2 = await workflow_service.get_workflow(wid)
        assert any(n["id"] == start["id"] for n in wf2["nodes"]), "起点必须还在"
        # 非起点仍然可以删
        other = next(n for n in wf2["nodes"] if n["id"] != start["id"])
        assert await workflow_service.delete_node(other["id"]) is True
        print("✅ 起点不可删（非起点照常可删）")
    finally:
        if wid:
            await workflow_service.delete_workflow(wid)
            print("已清理临时工作流", wid)
        pool.close()
        await pool.wait_closed()


asyncio.run(main())
