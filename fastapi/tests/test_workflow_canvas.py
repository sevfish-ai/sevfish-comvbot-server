"""工作流画布布局/连边回归测试（纯逻辑，不依赖 MySQL / 网络）

覆盖 workflow_engine 的执行语义映射到画布连边，以及分层布局：
- 顺序下一个 / 显式成功跳转 / 失败跳转 / switch 分支 + 兜底
- 跳转目标不存在时不能丢边，要带警示
- 分层单调（顺边一定向下），回跳边不参与分层（打断环）

运行：venv/bin/python -m pytest fastapi/tests/test_workflow_canvas.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.workflow import END_TARGET
from app.routes.workflows import (
    _jump_select,
    _node_form,
    _wf_canvas,
    _wf_edges,
    _wf_layout,
)


def _node(nid, name, ntype="llm", sort=0, succ="", fail="", params=None):
    return {
        "id": nid,
        "name": name,
        "node_type": ntype,
        "params": params or {},
        "sort": sort,
        "next_on_success": succ,
        "next_on_fail": fail,
    }


def _kinds(edges):
    return [(e["src"], e["dst"], e["label"]) for e in edges]


def _switch(nid, branches, default="", sort=1):
    return _node(
        nid,
        "pick",
        ntype="switch",
        sort=sort,
        params={"branches": branches, "default_next": default},
    )


def test_chain_uses_explicit_jumps_only():
    """顺序不参与运行语义：连成链必须靠节点自己写的跳转，不能靠"排在谁后面" """
    order = [
        _node(1, "a", sort=1, succ="b"),
        _node(2, "b", sort=2, succ="c"),
        _node(3, "c", sort=3),
    ]
    assert _kinds(_wf_edges(order)) == [(0, 1, "成功"), (1, 2, "成功"), (2, None, "成功")]


def test_empty_next_is_terminal_not_next_in_order():
    """空跳转 = 到此结束（不是"顺序下一个"）——这正是位置影响行为的老坑"""
    order = [_node(1, "a", sort=1), _node(2, "b", sort=2)]
    edges = _wf_edges(order)
    assert _kinds(edges) == [(0, None, "成功"), (1, None, "成功")]
    assert all(e["ended"] for e in edges)


def test_explicit_jumps_and_missing_target():
    order = [
        _node(1, "a", sort=1, succ="c", fail="b"),
        _node(2, "b", sort=2),
        _node(3, "c", sort=3, fail="ghost"),
    ]
    edges = _wf_edges(order)
    assert _kinds(edges)[0] == (0, 2, "成功")  # 显式成功跳转
    assert _kinds(edges)[1] == (0, 1, "失败")
    # 目标不存在 → 保留边 + 死引用提示 + 可重新挂节点（不能静默变成"结束"）
    last = edges[-1]
    assert (last["src"], last["dst"], last["label"]) == (2, None, "失败")
    assert "ghost" in last["dead"]


def test_switch_branches_and_default():
    order = [
        _node(
            1,
            "sw",
            ntype="switch",
            sort=1,
            params={
                "branches": [
                    {"label": "大于1", "op": ">", "value": "1", "next": "big"},
                    {"label": "", "next": "big"},  # 空分支名忽略
                    {"label": "等于0", "next": "zero"},
                ],
                "default_next": "big",
            },
        ),
        _node(2, "big", sort=2),
        _node(3, "zero", sort=3),
    ]
    edges = [e for e in _wf_edges(order) if e["src"] == 0]
    assert [e["label"] for e in edges] == ["大于1", "等于0", "兜底"]
    assert [e["dst"] for e in edges] == [1, 2, 1]
    assert edges[-1]["dashed"] is True  # 兜底虚线


def test_layout_is_layered_and_survives_back_edge():
    order = [
        _node(1, "a", sort=1, succ="c"),  # 跳过 b，再回跳到 a 形成环
        _node(2, "b", sort=2),
        _node(3, "c", sort=3, succ="a"),
    ]
    edges = _wf_edges(order)
    width, height, pos = _wf_layout(order, edges)
    assert set(pos) == {0, 1, 2}
    assert width > 0 and height > 0
    # 顺边一定向下：同层或更低说明分层错了
    for e in edges:
        if e["dst"] is not None and e["dst"] > e["src"]:
            assert pos[e["dst"]][1] > pos[e["src"]][1], e


def test_switch_empty_branch_does_not_fall_through():
    """分支留空不能"顺序续下"：多条空分支会一起续接到同一个节点，分不清节点归属"""
    order = [
        _node(
            1,
            "sw",
            ntype="switch",
            sort=1,
            params={
                "branches": [
                    {"label": "含退款", "next": ""},
                    {"label": "含订单", "next": "c"},
                ],
                "default_next": "",
            },
        ),
        _node(2, "b", sort=2),
        _node(3, "c", sort=3),
    ]
    edges = [e for e in _wf_edges(order) if e["src"] == 0]
    assert edges[0]["dst"] is None, "留空分支不该续接到 b"
    assert edges[0]["open"] is True and edges[0]["label"] == "含退款"
    assert edges[1]["dst"] == 2 and edges[1]["open"] is False
    assert edges[2]["dst"] is None and edges[2]["open"] is True  # 兜底留空


def test_switch_last_node_empty_branch_ends():
    """switch 是最后一个节点时，留空分支无处可去 → 画成结束标记且仍可挂节点"""
    order = [
        _node(1, "a", sort=1),
        _node(
            2,
            "sw",
            ntype="switch",
            sort=2,
            params={"branches": [{"label": "x", "next": ""}], "default_next": "a"},
        ),
    ]
    edges = [e for e in _wf_edges(order) if e["src"] == 1]
    assert edges[0]["dst"] is None and edges[0]["open"] is True
    assert edges[1]["dst"] == 0  # 兜底指回 a


def test_layout_does_not_stack_unreachable_nodes():
    """分支没接目标时，switch 后面的节点没有入边——不能和 switch 算到同一格叠在一起"""
    order = [
        _switch(1, [{"label": "a", "next": ""}, {"label": "b", "next": ""}]),
        _node(2, "tail", sort=2),
    ]
    edges = _wf_edges(order)
    _w, _h, pos = _wf_layout(order, edges)
    assert pos[0] != pos[1], pos


def test_each_open_branch_gets_its_own_add_button():
    """多个「接续向下」分支必须各有各的「＋」，否则没法决定新节点挂在哪条分支上"""
    order = [
        _switch(
            1,
            [
                {"label": "含退款", "next": ""},
                {"label": "含订单", "next": ""},
                {"label": "已指定", "next": "tail"},
            ],
        ),
        _node(2, "tail", sort=2),
    ]
    html = _wf_canvas(
        {"nodes": order}, workflow_id=7, oid_q="", oid_amp="", tables=[], agents=[], skills=[]
    )
    assert "insert?branch=0" in html  # 含退款
    assert "insert?branch=1" in html  # 含订单
    assert "insert?branch=-1" in html  # 兜底（留空）
    assert "insert?branch=2" not in html  # 已指定目标的分支不给 ＋，免得误覆盖
    assert html.count('class="wf-edgeadd"') == 3
    # switch 节点不给底部「＋」：它的走向由分支决定，挂个底部＋只会让人误解
    assert html.count('class="wf-add"') == 1  # 只有 tail


def test_add_button_url_keeps_org_scope():
    order = [_switch(11, [{"label": "a", "next": ""}]), _node(12, "tail", sort=2)]
    html = _wf_canvas(
        {"nodes": order},
        workflow_id=7,
        oid_q="?org_id=9",
        oid_amp="&org_id=9",
        tables=[],
        agents=[],
        skills=[],
    )
    assert "node/11/insert?branch=0&org_id=9" in html


def test_canvas_renders_nodes_and_edges():
    order = [
        _node(7, "q1", ntype="table_query", sort=1, fail="o1"),
        _node(8, "o1", ntype="output", sort=2),
    ]
    html = _wf_canvas(
        {"nodes": order}, workflow_id=3, oid_q="", oid_amp="", tables=[], agents=[], skills=[]
    )
    assert 'data-nid="7"' in html and 'data-nid="8"' in html
    assert 'id="wfViewport"' in html and 'id="wfForms"' in html
    assert "<path d=" in html  # 连边
    assert "@@" not in html  # 占位符必须全部替换掉


def test_canvas_handles_empty_workflow():
    html = _wf_canvas(
        {"nodes": []}, workflow_id=1, oid_q="?org_id=0", oid_amp="", tables=[], agents=[], skills=[]
    )
    assert "还没有节点" in html
    assert "@@" not in html

def test_end_target_is_a_clean_stop_not_an_open_port():
    """__end__ = 到此结束：画成结束标记且不给「＋」（和"没设目标"要区分开）"""
    order = [
        _node(1, "a", sort=1, succ=END_TARGET),
        _switch(2, [{"label": "X", "next": END_TARGET}], default=END_TARGET, sort=2),
        _node(3, "b", sort=3),
    ]
    edges = _wf_edges(order)
    a_edge = next(e for e in edges if e["src"] == 0)
    assert a_edge["dst"] is None and a_edge["ended"] and not a_edge["open"]
    sw_edges = [e for e in edges if e["src"] == 1]
    assert all(e["dst"] is None and e["ended"] and not e["open"] for e in sw_edges)
    assert not any(e["open"] for e in edges)
    html = _wf_canvas(
        {"nodes": order}, workflow_id=1, oid_q="", oid_amp="", tables=[], agents=[], skills=[]
    )
    assert "结束" in html
    assert 'class="wf-edgeadd"' not in html  # 没有 open 端口 → 没有分支「＋」


def test_branch_nodes_do_not_chain_each_other():
    """两条分支各挂一个收尾节点时，A 不能被"顺序下一个"串到 B 上"""
    order = [
        _switch(1, [{"label": "X", "next": "A"}, {"label": "Y", "next": "B"}]),
        _node(2, "A", sort=2, succ=END_TARGET),
        _node(3, "B", sort=3, succ=END_TARGET),
    ]
    edges = _wf_edges(order)
    assert next(e for e in edges if e["src"] == 0)["dst"] == 1  # X → A
    assert [e for e in edges if e["src"] == 0][1]["dst"] == 2  # Y → B
    assert all(e["dst"] is None for e in edges if e["src"] in (1, 2)), "A/B 都不该有后续边"


def test_start_node_is_marked_and_has_no_delete_link():
    """起点（id 最小的节点）不能删，所以画布上必须标出来、抽屉里不给删除按钮"""
    order = [_node(5, "first", sort=1), _node(9, "second", sort=2)]
    html = _wf_canvas(
        {"nodes": order}, workflow_id=3, oid_q="", oid_amp="", tables=[], agents=[], skills=[]
    )
    assert 'class="wf-start"' in html  # 起点徽章
    assert "▶ 起点" in html
    assert 'wf-is-start"' in html  # 起点节点带高亮 class
    # 起点没有删除链接，非起点有
    assert "/node/5/delete" not in html
    assert "/node/9/delete" in html
    assert "起点节点不能删除" in html


def test_jump_dropdowns_have_no_duplicate_end_option():
    """成功跳转里「空」和 __end__ 同义，不能同时渲染成两个「（到此结束）」"""
    succ = _jump_select("next_on_success", "", ["a", "b"], None, allow_end=True)
    assert succ.count("到此结束") == 1, succ
    assert succ.count("<option") == 3  # __end__ + a + b
    # 失败跳转没有"到此结束"，只有「（中止）」+ 节点
    fail = _jump_select("next_on_fail", "", ["a"], "（中止）")
    assert "到此结束" not in fail
    assert "（中止）" in fail
    # 兜底是两个不同含义的选项，各来一个
    dft = _jump_select("params[default_next]", "", ["a"], "（中止）", allow_end=True)
    assert dft.count("到此结束") == 1 and dft.count("（中止）") == 1


def test_jump_area_follows_node_type():
    """跳转区必须随类型变：switch 用分支定义，其它用成功/失败跳转。

    两块都渲染、由 showParams() 切换——只按初始类型渲染一块的话，把下拉切成
    「多分支判断」后屏幕上还留着成功/失败跳转。
    """
    llm = _node_form("/x", "llm", {}, [], name="n")
    sw = _node_form("/x", "switch", {}, [], name="n")
    for html in (llm, sw):
        assert html.count('class="njump"') == 2, "两块都要在，前端才能切"
    # 初始显示：llm 看到成功/失败跳转，switch 看到分支提示
    assert "flex-wrap:wrap;display:flex" in llm
    assert "min-width:100%;display:none" in llm
    assert "flex-wrap:wrap;display:none" in sw
    assert "min-width:100%;display:flex" in sw
