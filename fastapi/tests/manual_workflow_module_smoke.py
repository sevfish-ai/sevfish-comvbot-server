"""工作流模块端到端自检（真实 HTTP 路由 + 真实 MySQL）

覆盖这次大改动碰过的所有面：
- 工作流增删改查 / 开关 / 敏感标记 / 输入契约
- 14 种节点类型都能通过真实表单建出来（表单解析 → 落库）
- 编辑节点（含把类型从 llm 改成 switch，验证分支被正确解析）
- 画布渲染（起点徽章、无 /move 死链、无死引用、跳转下拉）
- 插入节点（普通插到后面 / 分支专属节点）
- 引擎语义（条件分支、switch 分支、兜底收尾、兜底留空报错、循环保护）
- 删除节点（清引用、起点不可删）
- 测试运行 + 运行日志页

设计：所有交互都走 HTTP（TestClient），断言用独立连接池里的**只读 SQL**——
服务层的 pool 单例归 app 自己的事件循环，测试进程直接调 service 会踩
"attached to a different loop"，所以不碰它。

运行：venv/bin/python fastapi/tests/manual_workflow_module_smoke.py
"""
import asyncio
import json
import os
import sys
from urllib.parse import urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiomysql
from fastapi.testclient import TestClient

import app.main as M
from app.config import settings
from app.models.workflow import AUTO_LINK, END_TARGET, NODE_TYPES

FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}
FAILS = []
_loop = asyncio.new_event_loop()
_pool = None


def check(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILS.append(msg)
    return cond


def _run(coro):
    return _loop.run_until_complete(coro)


def sql(query, args=None):
    async def go():
        async with _pool.acquire() as conn:
            async with conn.cursor() as cur:
                # 不传 args 时别走 pymysql 的 % 格式化：LIKE '__tmp_%' 里的 % 会被当成占位符
                if args:
                    await cur.execute(query, args)
                else:
                    await cur.execute(query)
                return await cur.fetchall()

    return _run(go())


def post(c, url, pairs):
    return c.post(url, content=urlencode(pairs), headers=FORM_HEADERS,
                  follow_redirects=False)


def ok303(r):
    """303 且带 ✅ 消息（模块里所有写操作都这么回）"""
    return r.status_code == 303 and "%E2%9C%85" in r.headers.get("location", "")


def err303(r):
    return r.status_code == 303 and "%E2%9D%8C" in r.headers.get("location", "")


def nodes(wid):
    rows = sql("SELECT id, name, node_type, params, next_on_success, next_on_fail "
               "FROM workflow_nodes WHERE workflow_id=%s ORDER BY id", (wid,))
    return [
        {"id": r[0], "name": r[1], "node_type": r[2], "params": json.loads(r[3] or "{}"),
         "next_on_success": r[4] or "", "next_on_fail": r[5] or ""}
        for r in rows
    ]


def create_wf(c, name):
    r = post(c, "/admin/workflow/create",
             [("name", name), ("description", "自检"), ("timeout_sec", "60")])
    assert ok303(r), f"创建工作流 {name} 失败: {r.headers.get('location')}"
    return sql("SELECT id FROM command_workflows WHERE name=%s", (name,))[0][0]


def run_and_get_trace(c, wid, text):
    """跑一次测试运行，返回 (status, [执行过的节点名])"""
    r = post(c, f"/admin/workflow/{wid}/test",
             [("agent_id", ""), ("robot_id", ""), ("user_text", text), ("simulate", "1")])
    assert r.status_code == 303, r.status_code
    rid = sql("SELECT id FROM workflow_runs WHERE workflow_id=%s ORDER BY id DESC LIMIT 1",
              (wid,))[0][0]
    status = sql("SELECT status FROM workflow_runs WHERE id=%s", (rid,))[0][0]
    trace = [r[0] for r in sql(
        "SELECT node_name FROM workflow_run_nodes WHERE run_id=%s ORDER BY id", (rid,))]
    return status, trace


def purge_tmp():
    for clause in (
        "workflow_run_nodes WHERE run_id IN (SELECT id FROM workflow_runs "
        "WHERE workflow_id IN (SELECT id FROM command_workflows WHERE name LIKE '__tmp_%'))",
        "workflow_runs WHERE workflow_id IN (SELECT id FROM command_workflows "
        "WHERE name LIKE '__tmp_%')",
        "workflow_nodes WHERE workflow_id IN (SELECT id FROM command_workflows "
        "WHERE name LIKE '__tmp_%')",
        "command_workflows WHERE name LIKE '__tmp_%'",
    ):
        sql(f"DELETE FROM {clause}")


def drop_wf(wid):
    sql("DELETE FROM workflow_run_nodes WHERE run_id IN "
        "(SELECT id FROM workflow_runs WHERE workflow_id=%s)", (wid,))
    sql("DELETE FROM workflow_runs WHERE workflow_id=%s", (wid,))
    sql("DELETE FROM workflow_nodes WHERE workflow_id=%s", (wid,))
    sql("DELETE FROM command_workflows WHERE id=%s", (wid,))


def run_checks(c, created):
    print("\n【1】工作流增删改查")
    wid = create_wf(c, "__tmp_smoke")
    created.append(wid)
    check(sql("SELECT COUNT(*) FROM command_workflows WHERE id=%s", (wid,))[0][0] == 1,
          "工作流已落库")
    r = c.get("/admin/workflows")
    check(r.status_code == 200 and "__tmp_smoke" in r.text, "列表页包含新工作流")

    check(post(c, f"/admin/workflow/{wid}/toggle", []).status_code == 303, "toggle 303")
    check(sql("SELECT enabled FROM command_workflows WHERE id=%s", (wid,))[0][0] == 0,
          "enabled 翻转成功")
    post(c, f"/admin/workflow/{wid}/toggle", [])

    post(c, f"/admin/workflow/{wid}/sensitive", [])
    check(sql("SELECT is_sensitive FROM command_workflows WHERE id=%s", (wid,))[0][0] == 1,
          "敏感标记翻转成功")
    post(c, f"/admin/workflow/{wid}/sensitive", [])

    r = post(c, f"/admin/workflow/{wid}/input_schema",
             [("sc_name[]", "user_text"), ("sc_type[]", "string"),
              ("sc_required[]", "1"), ("sc_desc[]", "用户输入")])
    check(ok303(r), "输入契约 303")
    schema = sql("SELECT input_schema FROM command_workflows WHERE id=%s", (wid,))[0][0] or ""
    check("user_text" in schema, f"输入契约已保存：{schema}")

    print("\n【2】14 种节点类型通过真实表单创建")
    payloads = {
        "llm": [("params[agent_id]", "0"), ("params[prompt]", "分析 {{input.user_text}}")],
        "table_query": [("params[table]", "t1"), ("cq_field[]", "a"), ("cq_op[]", "="),
                        ("cq_value[]", "1"), ("params[limit]", "10")],
        "table_insert": [("params[table]", "t1"), ("ir_field[]", "a"), ("ir_value[]", "1")],
        "table_update": [("params[table]", "t1"), ("params[id]", "1"),
                         ("uf_field[]", "a"), ("uf_value[]", "2")],
        "table_delete": [("params[table]", "t1"), ("params[id]", "1")],
        "table_create": [("params[name]", "临时表"), ("tf_name[]", "a"),
                         ("tf_type[]", "varchar"), ("tf_label[]", ""),
                         ("tf_required[]", "0")],
        "http_get": [("params[url]", "https://example.com"), ("hg_field[]", "k"),
                     ("hg_value[]", "v")],
        "http_post": [("params[url]", "https://example.com"), ("hpd_field[]", "k"),
                      ("hpd_value[]", "v"), ("hph_field[]", "h"), ("hph_value[]", "v")],
        "device_command": [("params[cmd_type]", "send_text"),
                           ("params[p_textType]", "text"), ("params[expect_type]", ""),
                           ("params[wait_sec]", "5")],
        "task_create": [("params[name]", "临时任务"), ("params[trigger_type]", "cron"),
                        ("params[trigger_cron]", "0 9 * * *"),
                        ("params[action_type]", "workflow"),
                        ("params[action_workflow_id]", str(wid))],
        "condition": [("params[field]", "input.user_text"), ("params[op]", "contains"),
                      ("params[value]", "x")],
        "switch": [("params[field]", "input.user_text"),
                   ("branch_label[]", "甲"), ("branch_op[]", "=="),
                   ("branch_value[]", "x"), ("branch_next[]", ""),
                   ("params[default_next]", END_TARGET)],
        "code": [("params[code]", "output['n'] = 1")],
        "output": [("params[text]", "结果 {{input.user_text}}")],
    }
    for nt, label, _d in NODE_TYPES:
        r = post(c, f"/admin/workflow/{wid}/node/add",
                 [("node_type", nt), ("name", f"n_{nt}")] + payloads.get(nt, []))
        check(ok303(r), f"{nt:<15}({label}) 创建成功 {r.headers.get('location','')[-46:]}")

    got = {n["name"]: n for n in nodes(wid)}
    for nt, _label, _d in NODE_TYPES:
        n = got.get(f"n_{nt}")
        check(n is not None and n["node_type"] == nt, f"{nt:<15} 落库类型正确")
    check(len(got) == len(NODE_TYPES), f"节点数 = {len(NODE_TYPES)}（实际 {len(got)}）")
    check(got["n_switch"]["params"]["branches"][0]["label"] == "甲",
          f"switch 分支被解析：{got['n_switch']['params'].get('branches')}")
    check(got["n_switch"]["params"]["default_next"] == END_TARGET, "switch 兜底被解析")
    check(got["n_table_query"]["params"].get("where") ==
          [{"field": "a", "op": "=", "value": "1"}],
          f"查表 where 被解析：{got['n_table_query']['params'].get('where')}")
    check(got["n_http_post"]["params"].get("headers") == {"h": "v"},
          f"http_post headers：{got['n_http_post']['params'].get('headers')}")
    check(bool(got["n_table_create"]["params"].get("fields")), "建表字段被解析")
    check(got["n_llm"]["params"].get("prompt") == "分析 {{input.user_text}}", "llm prompt 落库")

    print("\n【3】编辑节点：把 llm 改成 switch")
    sid = got["n_llm"]["id"]
    r = post(c, f"/admin/workflow/{wid}/node/{sid}/update",
             [("node_type", "switch"), ("name", "n_llm"),
              ("params[field]", "input.user_text"),
              ("branch_label[]", "甲的"), ("branch_op[]", "=="),
              ("branch_value[]", "x"), ("branch_next[]", ""),
              ("params[default_next]", END_TARGET)])
    check(ok303(r), "update 303")
    n = next(x for x in nodes(wid) if x["id"] == sid)
    check(n["node_type"] == "switch" and n["params"]["branches"][0]["label"] == "甲的",
          f"改成 switch 且分支被解析：{n['params'].get('branches')}")

    print("\n【4】画布页面渲染")
    r = c.get(f"/admin/workflow/{wid}")
    html = r.text
    check(r.status_code == 200, "详情页 200")
    check('class="wf-start"' in html, "起点徽章存在")
    check("/move?d=" not in html, "没有已删除的 /move 死链")
    check("（到此结束）" in html, "跳转下拉有「到此结束」")
    check(html.count('class="njump"') >= 2 * len(NODE_TYPES), "跳转区双块渲染")
    check("已不存在" not in html, "画布无死引用提示")
    check("顺序下一个" not in html, "页面不再出现「顺序下一个」")

    print("\n【5】插入节点")
    # 先接一条链 n_code → n_output，再往中间插，验证"继承原去向"
    code_id = got["n_code"]["id"]
    r = post(c, f"/admin/workflow/{wid}/node/{code_id}/update",
             [("node_type", "code"), ("name", "n_code"),
              ("params[code]", "output['n'] = 1"), ("next_on_success", "n_output")])
    check(ok303(r), "先把 n_code 接到 n_output")

    before = len(nodes(wid))
    r = c.get(f"/admin/workflow/{wid}/node/{code_id}/insert", follow_redirects=False)
    check(ok303(r), "普通插入 303")
    after = nodes(wid)
    check(len(after) == before + 1, f"节点数 {before} → {len(after)}")
    src = next(x for x in after if x["id"] == code_id)
    ins = next((x for x in after if x["name"] == src["next_on_success"]), None)
    check(ins is not None, f"源节点已接到新节点（{src['next_on_success']}）")
    check(ins is not None and ins["next_on_success"] == "n_output",
          f"新节点继承源节点原去向（{ins and ins['next_on_success']}）")

    # switch 必须有两条分支，才能验"只动被点的那条"
    sw = got["n_switch"]
    r = post(c, f"/admin/workflow/{wid}/node/{sw['id']}/update",
             [("node_type", "switch"), ("name", "n_switch"),
              ("params[field]", "input.user_text"),
              ("branch_label[]", "甲"), ("branch_op[]", "=="),
              ("branch_value[]", "x"), ("branch_next[]", END_TARGET),
              ("branch_label[]", "乙"), ("branch_op[]", "=="),
              ("branch_value[]", "y"), ("branch_next[]", END_TARGET),
              ("params[default_next]", END_TARGET)])
    check(ok303(r), "把 n_switch 改成两条分支")

    r = c.get(f"/admin/workflow/{wid}/node/{sw['id']}/insert?branch=0",
              follow_redirects=False)
    check(ok303(r), "分支插入 303")
    sw2 = next(x for x in nodes(wid) if x["id"] == sw["id"])
    br = sw2["params"]["branches"]
    check(len(br) == 2 and br[0]["next"] not in ("", END_TARGET), f"分支 0 已指向新节点（{br[0]['next']}）")
    check(br[1]["next"] == END_TARGET, f"分支 1 没被动（{br[1]['next']}）")
    check(sw2["next_on_success"] == "", "switch 自身的成功跳转没被动")
    check(sw2["params"]["default_next"] == END_TARGET, "兜底没被动")

    # switch 没有"下一个"，插在它后面应该被明确拒绝，而不是造出孤儿节点
    n_before = len(nodes(wid))
    r = c.get(f"/admin/workflow/{wid}/node/{sw['id']}/insert", follow_redirects=False)
    check(err303(r) and len(nodes(wid)) == n_before,
          f"拒绝「插到 switch 后面」（{r.headers.get('location','')[-40:]}）")

    print("\n【5b】新增节点：只有选「自动接在最后」才动上一个节点")
    w3 = create_wf(c, "__tmp_autolink")
    created.append(w3)
    post(c, f"/admin/workflow/{w3}/node/add",
         [("node_type", "output"), ("name", "A"), ("params[text]", "A"),
          ("next_on_success", END_TARGET)])
    post(c, f"/admin/workflow/{w3}/node/add",
         [("node_type", "output"), ("name", "B"), ("params[text]", "B"),
          ("next_on_success", END_TARGET)])
    n3 = {x["name"]: x for x in nodes(w3)}
    check(n3["A"]["next_on_success"] == END_TARGET,
          f"显式「到此结束」的 A 没被偷偷改（{n3['A']['next_on_success']}）")
    post(c, f"/admin/workflow/{w3}/node/add",
         [("node_type", "output"), ("name", "C"), ("params[text]", "C"),
          ("next_on_success", AUTO_LINK)])
    n3 = {x["name"]: x for x in nodes(w3)}
    check(n3["B"]["next_on_success"] == "C", f"选「自动接在最后」才把 B 接到 C（{n3['B']['next_on_success']}）")
    check(n3["C"]["next_on_success"] == END_TARGET, "新节点自身默认收尾")
    check(n3["A"]["next_on_success"] == END_TARGET, "A 依然没被动")

    print("\n【6】引擎语义（新建独立工作流，跑真实测试运行）")

    def scenario(name, node_specs, text):
        w2 = create_wf(c, name)
        created.append(w2)
        for nm, tp, fields in node_specs:
            r = post(c, f"/admin/workflow/{w2}/node/add",
                     [("node_type", tp), ("name", nm)] + fields)
            assert ok303(r), f"{name}/{nm} 创建失败 {r.headers.get('location')}"
        return run_and_get_trace(c, w2, text)

    def OUT(nm, nxt):
        return [("params[text]", nm), ("next_on_success", nxt)]

    def SW(frm, pairs, dft):
        fields = [("params[field]", frm)]
        for lbl, op, val, nxt in pairs:
            fields += [("branch_label[]", lbl), ("branch_op[]", op),
                       ("branch_value[]", val), ("branch_next[]", nxt)]
        return fields + [("params[default_next]", dft)]

    for text, expect in (("x", ["s", "sw", "ax"]), ("y", ["s", "sw", "ay"])):
        st, tr = scenario(f"__tmp_sw_{text}", [
            ("s", "output", OUT("s", "sw")),
            ("sw", "switch", SW("input.user_text",
                                [("X", "==", "x", "ax"), ("Y", "==", "y", "ay")],
                                END_TARGET)),
            ("ax", "output", OUT("ax", END_TARGET)),
            ("ay", "output", OUT("ay", END_TARGET)),
        ], text)
        check(st == "success" and tr == expect, f"switch 输入 {text!r} → {tr}（{st}）")

    st, tr = scenario("__tmp_sw_default", [
        ("s", "output", OUT("s", "sw")),
        ("sw", "switch", SW("input.user_text", [("X", "==", "x", "ax")], END_TARGET)),
        ("ax", "output", OUT("ax", END_TARGET)),
    ], "zzz")
    check(st == "success" and tr == ["s", "sw"], f"都没命中 → 兜底收尾 {tr}（{st}）")

    st, tr = scenario("__tmp_sw_open", [
        ("s", "output", OUT("s", "sw")),
        ("sw", "switch", SW("input.user_text", [("X", "==", "x", "ax")], "")),
        ("ax", "output", OUT("ax", END_TARGET)),
    ], "zzz")
    check(st == "failed" and tr == ["s", "sw"], f"兜底留空 → 报错中止 {tr}（{st}）")

    st, tr = scenario("__tmp_loop", [
        ("a", "output", OUT("a", "b")),
        ("b", "output", OUT("b", "a")),
    ], "x")
    check(st == "loop" and tr == ["a", "b"], f"往回跳判循环 {tr}（{st}）")

    cond = [
        ("a", "output", OUT("a", "c")),
        ("c", "condition", [("params[field]", "input.user_text"), ("params[op]", "=="),
                            ("params[value]", "x"), ("next_on_success", "ok"),
                            ("next_on_fail", "no")]),
        ("ok", "output", OUT("ok", END_TARGET)),
        ("no", "output", OUT("no", END_TARGET)),
    ]
    st, tr = scenario("__tmp_cond_ok", list(cond), "x")
    check(st == "success" and tr == ["a", "c", "ok"], f"条件节点满足分支 {tr}（{st}）")
    st, tr = scenario("__tmp_cond_no", list(cond), "zzz")
    check(st == "success" and tr == ["a", "c", "no"], f"条件节点不满足走失败跳转 {tr}（{st}）")

    print("\n【7】测试运行 + 运行日志页")
    st, tr = run_and_get_trace(c, wid, "x")
    check(st in ("success", "failed", "loop", "timeout"), f"测试运行有结果（{st}）")
    rid = sql("SELECT id FROM workflow_runs WHERE workflow_id=%s ORDER BY id DESC LIMIT 1",
              (wid,))[0][0]
    r = c.get(f"/admin/workflow/{wid}/run/{rid}")
    check(r.status_code == 200, "运行详情页 200")

    print("\n【8】删除节点")
    start = min(nodes(wid), key=lambda x: x["id"])
    r = c.get(f"/admin/workflow/{wid}/node/{start['id']}/delete", follow_redirects=False)
    check(r.status_code == 303, "删起点没炸成 500")
    check(err303(r), f"删起点返回 ❌ 提示 {r.headers.get('location','')[-50:]}")
    check(any(x["id"] == start["id"] for x in nodes(wid)), "起点还在")

    target = got["n_switch"]
    before_names = {x["name"] for x in nodes(wid)}
    r = c.get(f"/admin/workflow/{wid}/node/{target['id']}/delete", follow_redirects=False)
    check(ok303(r), "删普通节点 303")
    after = nodes(wid)
    names = {x["name"] for x in after}
    check(target["name"] not in names, "目标节点已删除")
    check(before_names - names == {target["name"]}, "只删掉了目标节点")

    dangling = []
    for x in after:
        v = (x["next_on_success"] or "").strip()
        if v and v != END_TARGET and v not in names:
            dangling.append((x["name"], "next_on_success", v))
        for br in (x["params"].get("branches") or []):
            nv = str(br.get("next") or "").strip()
            if nv and nv != END_TARGET and nv not in names:
                dangling.append((x["name"], "branch", nv))
        dv = str(x["params"].get("default_next") or "").strip()
        if dv and dv != END_TARGET and dv not in names:
            dangling.append((x["name"], "default_next", dv))
    check(not dangling, f"删除后无死引用（{dangling}）")

    print("\n【9】删除工作流")
    r = c.get(f"/admin/workflow/{wid}/delete", follow_redirects=False)
    check(r.status_code == 303, "删除工作流 303")
    check(sql("SELECT COUNT(*) FROM command_workflows WHERE id=%s", (wid,))[0][0] == 0,
          "工作流已删除")
    check(sql("SELECT COUNT(*) FROM workflow_nodes WHERE workflow_id=%s", (wid,))[0][0] == 0,
          "节点已级联删除")
    created.remove(wid)


def main():
    global _pool

    async def mkpool():
        return await aiomysql.create_pool(
            host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
            password=settings.MYSQL_PASSWORD, db=settings.MYSQL_DATABASE,
            autocommit=True, minsize=1, maxsize=4)

    with TestClient(M.app) as c:
        _pool = _run(mkpool())
        created = []
        try:
            purge_tmp()
            run_checks(c, created)
        finally:
            for wid in created:
                drop_wf(wid)
            print(f"\n已清理 {len(created)} 条临时工作流")

    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败：")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
