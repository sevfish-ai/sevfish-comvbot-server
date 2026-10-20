"""全站页面/表单冒烟扫描（手工脚本，pytest 不收集）

为什么存在：长期记忆接入时漏了 `GroupService._MEMBER_TYPES` 白名单，
`/admin/agents?org_id=1` 直接 500 —— 而当时只冒烟了 `org_id=0`，测不出来。
这里的做法是：**枚举 app 里所有已注册的 GET 路由**，用真实组织上下文逐个请求，
再对带写入语义的表单做一次真实 POST 流程（含"未渲染记忆区时不得清空绑定"）。

用法：
    cd fastapi && ../venv/bin/python tests/manual_page_sweep.py [-v]

只创建 id ≥ 990000 的临时数据并自行清理，不动真实数据；
带删除/启停/执行语义的路由会被跳过（不扫，避免误删）。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiomysql  # noqa: E402
import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import (  # noqa: E402
    agent_service,
    ext_user_service,
    group_service,
    knowledge_service,
    memory_service,
    robot_service,
    skill_service,
    table_service,
    task_service,
    workflow_service,
)

VERBOSE = "-v" in sys.argv

# 会被跳过的路由关键词：这些 GET 会改数据（删除/启停/执行/导出）
# 按**路径段**判断，不按子串：早先按子串写过 "kill"，把 /admin/skills 也当成危险路由跳过了
# （又一次静默漏测）。段名精确匹配，宁多写几个词也不误伤。
DANGEROUS_SEGMENTS = {
    "delete", "toggle", "clear", "test_run", "reset", "remove", "run",
    "start", "stop", "apply", "sync", "migrate", "ban", "unban", "logout",
    "export", "kill", "kick", "unbind", "restart", "shutdown", "close", "download",
}


def is_dangerous(path: str) -> bool:
    return any(seg in DANGEROUS_SEGMENTS for seg in path.strip("/").split("/"))

T_AGENT = 990101
T_SET = 990101
T_ROBOT = "zz990101"
T_ORG_NAME = "zz全站冒烟组织"
T_SET_NAME = "zz冒烟记忆集"


class Report:
    def __init__(self):
        self.ok = []
        self.fail = []
        self.skip = []

    def add(self, name, status, detail=""):
        row = (name, status, detail)
        if status in ("pytestfail",):
            self.fail.append(row)
        elif status == "skip":
            self.skip.append(row)
        else:
            self.ok.append(row)
        if VERBOSE or status not in ("200", "303", "302", "skip"):
            print(f"  [{status}] {name} {detail[:160]}")


def iter_api_routes(routes, out=None):
    """递归展开 app.routes。

    这个 FastAPI 版本把 include_router 的结果包成 _IncludedRouter（没有 .path），
    直接遍历 app.routes 会**静默漏掉几乎所有业务路由** —— 本脚本第一版就是这么
    "14 个路径全部通过"的，和被测代码犯的是同一个错。
    """
    if out is None:
        out = []
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            iter_api_routes(getattr(inner, "routes", []), out)
            continue
        sub = getattr(r, "routes", None)
        if sub and not getattr(r, "path", ""):
            iter_api_routes(sub, out)
            continue
        if getattr(r, "path", ""):
            out.append(r)
    return out


async def main() -> int:
    pool = await aiomysql.create_pool(
        host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD, db=settings.MYSQL_DATABASE,
        charset="utf8mb4", autocommit=True,
    )
    for svc in (agent_service, ext_user_service, group_service, knowledge_service,
                memory_service, robot_service, skill_service, table_service,
                task_service, workflow_service):
        svc.set_pool(pool)
        await svc.ensure_table()

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            async def one(sql, default=0):
                await cur.execute(sql)
                row = await cur.fetchone()
                return row[0] if row and row[0] is not None else default

            real_org = await one("SELECT id FROM org_groups ORDER BY id LIMIT 1")
            real_agent = await one("SELECT id FROM agents ORDER BY id LIMIT 1")
            real_robot = await one("SELECT robot_id FROM robots ORDER BY robot_id LIMIT 1", "")
            real_wf = await one("SELECT id FROM command_workflows ORDER BY id LIMIT 1")
            real_run = await one("SELECT id FROM workflow_runs ORDER BY id DESC LIMIT 1")
            real_run_node = await one("SELECT id FROM workflow_nodes ORDER BY id LIMIT 1")
            real_task = await one("SELECT id FROM tasks ORDER BY id LIMIT 1")
            real_skill = await one("SELECT id FROM skills ORDER BY id LIMIT 1")
            real_kb = await one("SELECT id FROM knowledge_bases ORDER BY id LIMIT 1")
            real_doc = await one("SELECT id FROM knowledge_documents ORDER BY id LIMIT 1")
            real_table = await one("SELECT id FROM custom_tables ORDER BY id LIMIT 1")
            real_key = await one("SELECT table_key FROM custom_tables ORDER BY id LIMIT 1", "")
            real_uid = await one("SELECT id FROM ext_users ORDER BY id LIMIT 1")

    org_id = await group_service.create_org_group(T_ORG_NAME)
    rep = Report()
    try:
        # ── 造临时数据：组织 + 智能体 + 绑定设备（为了 robot_count>0）+ 记忆集 ──
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM agents WHERE id=%s", (T_AGENT,))
                await cur.execute("DELETE FROM robots WHERE robot_id=%s", (T_ROBOT,))
                await cur.execute(
                    """INSERT INTO agents (id,name,description,provider,base_url,model,api_key,
                                           system_prompt,enabled)
                       VALUES (%s,'zz冒烟智能体','','custom','http://127.0.0.1:1','m','k','ZZ_SYS',1)""",
                    (T_AGENT,),
                )
                await cur.execute(
                    "INSERT INTO robots (robot_id,token,device_name,agent_id) VALUES (%s,'t','zz冒烟设备',%s)",
                    (T_ROBOT, T_AGENT),
                )
                await conn.commit()
        if await memory_service.get_set(T_SET):
            await memory_service.delete_set(T_SET)
        await memory_service.create_set(T_SET_NAME, "扫描用")
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE memory_sets SET id=%s WHERE name=%s", (T_SET, T_SET_NAME))
                await conn.commit()
        await group_service.set_member_org_groups("memory", T_SET, [org_id])
        await group_service.add_group_org_member(org_id, "agent", T_AGENT)
        await group_service.add_group_org_member(org_id, "device", T_ROBOT)
        await memory_service.set_robot_memory_sets(T_ROBOT, write_id=T_SET, read_ids=[])
        cid = await memory_service.add_customer(T_SET, "zz客户甲", "zz甲-采购")
        await memory_service.add_item(T_SET, cid, "zz事实一")
        await memory_service.add_item(T_SET, 0, "zz通用事实")
        items = await memory_service.list_items(T_SET)
        iid = items[0]["id"]

        params = {
            "org_id": org_id, "agent_id": T_AGENT, "robot_id": T_ROBOT,
            "client_id": T_ROBOT, "view": T_SET, "set_id": T_SET,
            "customer_id": cid, "item_id": iid,
            "kb_id": real_kb, "doc_id": real_doc,
            "workflow_id": real_wf, "run_id": real_run, "node_id": real_run_node, "task_id": real_task,
            "skill_id": real_skill, "user_id": real_uid, "uid": real_uid,
            "table_id": real_table, "key": real_key, "table_key": real_key,
            "name": "zz", "msg": "", "path": "", "q": "zz",
        }
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT id FROM org_groups ORDER BY id LIMIT 3")
                org_ctx = [org_id] + [r[0] for r in await cur.fetchall() if r[0] != org_id]
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as cli:
            # ── 1. 枚举全部 GET 路由并逐个请求 ──
            all_routes = iter_api_routes(app.routes)
            get_routes = [
                r for r in all_routes
                if "GET" in (getattr(r, "methods", None) or set())
            ]
            print(f"枚举到路由 {len(all_routes)} 条，其中 GET {len(get_routes)} 条")
            # 自检：扫到的路由数太少直接判定脚本坏了（防"静默空跑全部通过"）
            if len(get_routes) < 40:
                rep.add("扫描器自检(路由枚举)", "pytestfail",
                        f"只枚举到 {len(get_routes)} 条 GET 路由，脚本可能没展开 include_router")

            seen = set()
            for route in get_routes:
                path = route.path
                if path in seen:
                    continue
                seen.add(path)
                if is_dangerous(path):
                    rep.add(f"GET {path}", "skip", "改数据/危险路由，跳过")
                    continue
                url, missing = path, False
                for part in [p for p in path.split("/") if p.startswith("{")]:
                    key = part.strip("{}").split(":")[0]
                    val = params.get(key)
                    if val in (None, "", 0):
                        missing = True
                        break
                    url = url.replace(part, str(val))
                if missing:
                    rep.add(f"GET {path}", "skip", "缺少可用的路径参数样例")
                    continue
                variants = [url]
                if "org_id" not in url:
                    # 带组织上下文再打一遍：`?org_id=` 分支会走 org_group_members 白名单
                    variants += [f"{url}{'&' if '?' in url else '?'}org_id={o}" for o in org_ctx]
                for v in variants:
                    try:
                        r = await cli.get(v)
                        if r.status_code >= 400:
                            rep.add(f"GET {v}", "pytestfail", f"HTTP {r.status_code}: {r.text[:200]}")
                        else:
                            rep.add(f"GET {v}", str(r.status_code))
                    except Exception as e:
                        rep.add(f"GET {v}", "pytestfail", f"{type(e).__name__}: {e}")

            # ── 2. 关键页面内容断言（渲染出来的东西对不对）──
            r = await cli.get(f"/admin?org_id={org_id}")
            checks = [
                ("设备页含长期记忆列", "长期记忆" in r.text),
                ("设备页含记忆设置入口", f"/admin/robot/{T_ROBOT}/memory" in r.text),
            ]
            r = await cli.get(f"/admin/robot/{T_ROBOT}/memory?org_id={org_id}")
            checks += [
                ("设备记忆页含维护集单选", 'name="memory_write"' in r.text),
                ("设备记忆页含只读集多选", 'name="memory_read_ids"' in r.text),
                ("设备记忆页含该记忆集", f'value="{T_SET}"' in r.text),
            ]
            r = await cli.get(f"/admin/agents?org_id={org_id}")
            checks.append(("智能体页已不含记忆勾选", "memory_write" not in r.text))
            r2 = await cli.get(f"/admin/memory?org_id={org_id}&view={T_SET}")
            checks += [
                ("记忆详情含客户", "zz客户甲" in r2.text),
                ("记忆详情含别名", "zz甲-采购" in r2.text),
                ("记忆详情含事实", "zz事实一" in r2.text),
                # 打开记忆集时页面上只有外侧这一份护栏（内层那份已删）
                ("记忆详情含护栏且只有一份", r2.text.count("🛡️ 护栏与") == 1
                 and "本套当前用量" not in r2.text),
                ("记忆详情含通用事实", "zz通用事实" in r2.text),
            ]
            r3 = await cli.get("/admin/memory")
            checks.append(("记忆列表页含护栏(不打开详情也可见)",
                           r3.text.count("🛡️ 护栏与") == 1
                           and "单条事实" in r3.text and "200 字" in r3.text))
            r3 = await cli.get(f"/admin/org/{org_id}/members")
            checks.append(("组织成员页含记忆集", T_SET_NAME in r3.text))
            for name, ok in checks:
                rep.add(name, "200" if ok else "pytestfail", "" if ok else "内容缺失")

            # ── 3. POST 表单流（记忆 CRUD 全走 HTTP，不直接调 service）──
            base = f"/admin/memory/{T_SET}"
            q = f"?org_id={org_id}"

            def post(url, data):
                return cli.post(url + q, data=data)

            r = await post("/admin/memory/create", {"name": "zz扫描新建集", "description": ""})
            rep.add("POST 记忆集创建", str(r.status_code))
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT id FROM memory_sets WHERE name='zz扫描新建集'")
                    new_set = (await cur.fetchone())
            if new_set:
                rep.add("POST 记忆集创建落库", "200" if new_set else "pytestfail")
                r = await cli.get(f"/admin/memory/set/{new_set[0]}/delete?org_id={org_id}")
                rep.add("GET 记忆集删除", str(r.status_code))
                async with pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT id FROM memory_sets WHERE id=%s", (new_set[0],))
                        gone = not await cur.fetchone()
                rep.add("GET 记忆集删除生效", "200" if gone else "pytestfail")
            else:
                rep.add("POST 记忆集创建落库", "pytestfail", "库里没有新建的记忆集")

            r = await post(f"{base}/update", {"name": T_SET_NAME + "改", "description": "改了"})
            rep.add("POST 记忆集改名", str(r.status_code))
            st = await memory_service.get_set(T_SET)
            rep.add("POST 记忆集改名生效", "200" if st and st["name"].endswith("改") else "pytestfail")

            r = await post(f"{base}/customer/create", {"name": "zz客户乙", "aliases": "zz乙,乙总"})
            rep.add("POST 新增客户", str(r.status_code))
            hit = await memory_service.resolve_customer(T_SET, "乙总")
            rep.add("POST 新增客户别名可命中", "200" if hit else "pytestfail")
            cid2 = hit[0] if hit else 0

            r = await post(f"{base}/customer/{cid2}/update", {"name": "zz客户乙", "aliases": "乙总,乙老板"})
            rep.add("POST 改客户别名", str(r.status_code))
            hit = await memory_service.resolve_customer(T_SET, "乙老板")
            rep.add("POST 改别名后命中", "200" if hit else "pytestfail")

            r = await post(f"{base}/item/create", {"customer_id": cid2, "content": "zz事实乙"})
            rep.add("POST 加事实", str(r.status_code))
            r = await post(f"{base}/item/create", {"customer_id": cid2, "content": "zz事实乙"})
            rep.add("POST 重复事实(应去重)", str(r.status_code))
            n = len([i for i in await memory_service.list_items(T_SET) if i["content"] == "zz事实乙"])
            rep.add("POST 事实去重生效", "200" if n == 1 else "pytestfail", f"条数={n}")

            r = await post(f"{base}/import", {"customer_name": "zz客户丙", "text": "zz导入1\nzz导入2\nzz事实一"})
            rep.add("POST 批量导入", str(r.status_code))
            n3 = len([i for i in await memory_service.list_items(T_SET) if i["content"].startswith("zz导入")])
            rep.add("POST 导入落库", "200" if n3 == 2 else "pytestfail", f"条数={n3}")

            r = await post(f"{base}/customer/{cid2}/merge", {"dst_id": cid})
            rep.add("POST 合并客户", str(r.status_code))
            rep.add("POST 合并后别名可回查", "200" if await memory_service.resolve_customer(T_SET, "乙老板") else "pytestfail")

            items = await memory_service.list_items(T_SET)
            iid2 = items[0]["id"]
            r = await post(f"{base}/item/{iid2}/update", {"content": "zz事实改过了"})
            rep.add("POST 改事实", str(r.status_code))
            rep.add("POST 改事实生效", "200" if any(i["content"] == "zz事实改过了" for i in await memory_service.list_items(T_SET)) else "pytestfail")
            r = await cli.get(f"/admin/memory/item/{iid2}/delete?set_id={T_SET}&view={T_SET}&org_id={org_id}")
            rep.add("GET 删事实", str(r.status_code))
            r = await cli.get(f"/admin/memory/customer/{cid2}/delete?set_id={T_SET}&view={T_SET}&org_id={org_id}")
            rep.add("GET 删客户", str(r.status_code))

            # ── 4. 设备页绑定记忆（真实 HTTP）：保存生效 + 换智能体不影响记忆 ──
            r = await cli.post(
                f"/admin/robot/{T_ROBOT}/memory?org_id={org_id}",
                data={"memory_write": str(T_SET)},
            )
            rep.add("POST 设备记忆绑定", str(r.status_code))
            link = await memory_service.get_robot_memory_sets(T_ROBOT)
            rep.add("设备记忆绑定生效", "200" if link["write"] == T_SET else "pytestfail", str(link))

            r = await cli.post(
                f"/admin/robot/{T_ROBOT}/memory?org_id={org_id}",
                data={"memory_write": "0"},
            )
            rep.add("POST 设备记忆改为不维护", str(r.status_code))
            rep.add("改不维护后绑定清空（数据仍在）",
                    "200" if (await memory_service.get_robot_memory_sets(T_ROBOT))["write"] is None
                    and await memory_service.get_set(T_SET) else "pytestfail")

            # 换绑智能体不影响设备的记忆绑定
            await cli.post(
                f"/admin/robot/{T_ROBOT}/memory?org_id={org_id}", data={"memory_write": str(T_SET)}
            )
            r = await cli.post(f"/admin/robot/{T_ROBOT}/agent?org_id={org_id}", data={"agent_id": "0"})
            rep.add("POST 设备解绑智能体", str(r.status_code))
            rep.add("解绑智能体后记忆绑定仍在",
                    "200" if (await memory_service.get_robot_memory_sets(T_ROBOT))["write"] == T_SET
                    else "pytestfail")
    finally:
        # ── 清理临时数据 ──
        await memory_service.set_robot_memory_sets(T_ROBOT, 0, [])
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM memory_items WHERE set_id=%s", (T_SET,))
                await cur.execute("DELETE FROM memory_customers WHERE set_id=%s", (T_SET,))
                await cur.execute("DELETE FROM memory_sets WHERE id=%s OR name LIKE 'zz%%'", (T_SET,))
                await cur.execute("DELETE FROM agents WHERE id=%s", (T_AGENT,))
                await cur.execute("DELETE FROM robots WHERE robot_id=%s", (T_ROBOT,))
                await cur.execute("DELETE FROM agent_conversations WHERE robot_id=%s", (T_ROBOT,))
                await conn.commit()
        await group_service.delete_org_group(org_id)
        pool.close()
        await pool.wait_closed()

    print(f"\n{'='*60}\nGET/POST 通过 {len(rep.ok)}｜失败 {len(rep.fail)}｜跳过 {len(rep.skip)}")
    if rep.fail:
        print("\n失败清单：")
        for name, _s, detail in rep.fail:
            print(f"  ✗ {name} — {detail}")
    else:
        print("全部通过 ✅")
    return 1 if rep.fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
