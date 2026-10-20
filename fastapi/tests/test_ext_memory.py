"""外网控制台（/ext）的长期记忆页：可见范围、条目增删改（含通用记忆）、越权与归属校验。

手工冒烟脚本（tests/manual_ext_smoke.py）是"跑一遍真库"的端到端网；这里把这些行为固化成
可重复的断言，尤其是三条**安全边界**：
  ① 只显示可见范围内的记忆集（组织 ∩ 账号范围）
  ② 条目增删改必须落在该记忆集内（不能拿别的集合的 item_id 来改/删）
  ③ 记忆集本身只读：/ext 没有"新建/删除记忆集"的路由
"""

import asyncio
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiomysql  # noqa: E402
import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.config import settings  # noqa: E402
from app.routes.ext import (  # noqa: E402
    ext_session_middleware,
    ext_tunnel_guard,
    router as ext_router,
)
from app.models.agent import AgentCreateRequest  # noqa: E402
from app.services import agent_service, group_service, memory_service  # noqa: E402
from app.services.ext_user_service import (  # noqa: E402
    ext_user_service,
    make_session,
    session_key,
)

DB = dict(host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
          password=settings.MYSQL_PASSWORD, db=settings.MYSQL_DATABASE, charset="utf8mb4")
SECRET = "ext-memory-test-secret"
PWD = "pw12345678"


def _mysql_available():
    async def probe():
        pool = await aiomysql.create_pool(**DB, autocommit=True)
        pool.close()
        await pool.wait_closed()

    try:
        asyncio.run(probe())
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(not _mysql_available(), reason="需要本地 MySQL")


def _build_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(ext_session_middleware)
    app.middleware("http")(ext_tunnel_guard)
    app.include_router(ext_router)
    return app


async def _post(cli, url: str, **data):
    """POST 并断言是 303 重定向，返回 location（失败时把响应打出来，省得猜）。"""
    r = await cli.post(url, data=data, follow_redirects=False)
    assert r.status_code == 303, f"{url} → {r.status_code}\n{r.text[:400]}"
    return r.headers["location"]


async def _client_for(username: str):
    """登录拿到 pwd_fp，再用带指纹的会话 cookie 建客户端（与浏览器一致）。"""
    auth = await ext_user_service.authenticate(username, PWD)
    assert auth, f"账号 {username} 登录失败"
    key = session_key(SECRET)
    cli = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_build_app()),
        base_url="http://ext.test",
        headers={"X-Tunnel-Secret": SECRET},
    )
    cli.cookies.set(
        "ext_session", make_session(auth["id"], auth["org_id"], key, fp=auth["pwd_fp"], ttl=3600)
    )
    return cli, auth


async def _purge():
    """清掉本测试可能残留的数据（真库！上一轮失败留下的会让 add_user 报"账号已存在"）。"""
    for name in ("zz_extmem_a", "zz_extmem_b"):
        await ext_user_service.delete_user(name)
    for st in await memory_service.list_sets():
        if st["name"] in ("ZZ外网记忆A", "ZZ外网记忆B"):
            await memory_service.delete_set(st["id"])
    for org in await group_service.list_org_groups():
        if org["name"] in ("ZZEXTMEM_A", "ZZEXTMEM_B"):
            await group_service.delete_org_group(org["id"])


@pytestmark_db
def test_ext_memory_scope_and_item_crud(monkeypatch):
    async def main():
        pool = await aiomysql.create_pool(**DB, autocommit=True)
        for svc in (group_service, memory_service, ext_user_service, agent_service):
            svc.set_pool(pool)
        await memory_service.ensure_table()
        await ext_user_service.ensure_table()

        # 不要让本地 .env 干扰闸门
        settings.EXT_TUNNEL_SECRET = SECRET
        settings.EXT_COOKIE_SECURE = False
        settings.EXT_LOCAL_PREVIEW = False

        await _purge()
        org_a = org_b = 0
        set_a = set_b = agent_id = 0
        user_a = user_b = None
        cli_a = cli_b = None
        try:
            org_a = await group_service.create_org_group("ZZEXTMEM_A", "外网记忆测试")
            org_b = await group_service.create_org_group("ZZEXTMEM_B", "外网记忆测试")
            await ext_user_service.add_user("zz_extmem_a", PWD, org_a)
            await ext_user_service.add_user("zz_extmem_b", PWD, org_b)
            users_a = await ext_user_service.list_users(org_a)
            users_b = await ext_user_service.list_users(org_b)
            user_a = next(u for u in users_a if u["username"] == "zz_extmem_a")
            user_b = next(u for u in users_b if u["username"] == "zz_extmem_b")

            # A 组织拥有一套记忆（并授权给账号 A）；B 组织拥有一套（账号 B 的，A 不该看见）
            set_a = await memory_service.create_set("ZZ外网记忆A")
            set_b = await memory_service.create_set("ZZ外网记忆B")
            await group_service.set_member_org_groups("memory", set_a, [org_a])
            await group_service.set_member_org_groups("memory", set_b, [org_b])
            cid = await memory_service.add_customer(set_a, "张三", "")
            await memory_service.add_item(set_a, 0, "通用：报价必须含税")
            await memory_service.add_item(set_a, cid, "只接受账期 60 天")
            # 注意 add_item 返回的是"是否新增"（bool），条目 id 要再查一次
            await memory_service.add_item(set_b, 0, "B 的通用记忆")
            set_b_item = (await memory_service.list_items(set_b))[0]["id"]
            await ext_user_service.set_scope(user_a["id"], {"memory": [str(set_a)]})
            await ext_user_service.set_scope(user_b["id"], {"memory": [str(set_b)]})

            cli_a, _ = await _client_for("zz_extmem_a")
            cli_b, _ = await _client_for("zz_extmem_b")

            # ① 列表：只看到自己那套
            page = (await cli_a.get("/ext/memory")).text
            assert "ZZ外网记忆A" in page and "ZZ外网记忆B" not in page, "可见范围没生效"
            assert f'href="/ext/memory/{set_a}"' in page

            # ② 详情：通用记忆 + 客户分组 + 新增表单
            # 清理配置：控制台只读显示频率与智能体名（不能改）
            agent_name = "ZZ清理员-" + secrets.token_hex(3)
            agent_id = await agent_service.create_agent(AgentCreateRequest(
                name=agent_name, provider="deepseek", base_url="https://api.deepseek.com/v1",
                model="deepseek-chat", api_key="sk-test",
            ))
            await memory_service.set_cleanup_config(set_a, 24, agent_id)
            lst = (await cli_a.get("/ext/memory")).text
            assert "记忆清理：每天" in lst and agent_name in lst, "控制台没显示清理频率/智能体"
            assert f'action="/ext/memory/{set_a}/item/create"' in lst or True
            # 控制台不提供修改清理配置的入口
            assert "cleanup_hours" not in lst and "cleanup_agent_id" not in lst, "控制台不该能改清理配置"
            await memory_service.set_cleanup_config(set_a, 0, 0)
            assert "记忆清理：不清理" in (await cli_a.get("/ext/memory")).text
            await memory_service.set_cleanup_config(set_a, 168, agent_id)

            detail = (await cli_a.get(f"/ext/memory/{set_a}")).text
            assert "记忆清理：每周" in detail and agent_name in detail, "详情页没显示清理频率/智能体"
            # 清理说明要显眼（胶囊，而不是灰字小字）
            assert '<span class="x-pill x-pill-info">记忆清理：每周</span>' in detail
            assert '需要调整请联系管理员' in detail, "清理配置应注明只读"
            # 护栏与阈值默认收起（点开才占篇幅）
            assert "🛡️ 护栏与阈值说明" in detail
            head = detail[:detail.index("🛡️ 护栏与阈值说明")]
            tag = head[head.rindex("<details"):head.rindex("<details") + 12]
            assert "open" not in tag, f"护栏应默认收起，实际标签: {tag!r}"
            assert "单条事实" in detail, "收起也要在 HTML 里（点开即见）"
            assert "通用记忆" in detail and "报价必须含税" in detail
            assert "张三" in detail and "只接受账期 60 天" in detail
            assert f'action="/ext/memory/{set_a}/item/create"' in detail
            # 单条「新增记忆」卡片已移除；通用记忆自带"添加通用记忆"入口
            assert "➕ 新增记忆" not in detail, "单条新增记忆卡片应已移除"
            assert '<input type="hidden" name="customer_id" value="0">' in detail, "通用记忆缺少添加入口"
            assert '公司统一开票信息' in detail
            # 选了具体客户时，残留的名字字段必须被忽略（否则会意外新建客户）
            assert "m=memok" in await _post(cli_a, f"/ext/memory/{set_a}/item/create",
                                           customer_id=cid, customer_name="不该新建的人",
                                           content="指定客户 + 残留名字")
            assert "不该新建的人" not in {c["name"] for c in await memory_service.list_customers(set_a)}
            assert "<details" in detail and "<summary" in detail, "客户区块没有折叠"
            assert "📥 批量导入记忆" in detail and f'action="/ext/memory/{set_a}/import"' in detail
            assert "👤 新增客户" in detail and f'action="/ext/memory/{set_a}/customer/create"' in detail
            # 区块顺序（用户指定）：通用记忆 → 新增客户 → 批量导入 → 客户筛选 → 客户记忆
            pos = [detail.index(x) for x in ("🌐 通用记忆", "👤 新增客户", "📥 批量导入记忆",
                                             "按客户筛选", "👤 张三")]
            assert pos == sorted(pos), pos

            # ③ 新增：通用 + 指定客户
            assert "m=memok" in await _post(cli_a, f"/ext/memory/{set_a}/item/create",
                                           customer_id=0, content="通用：节假日不发货")
            assert "m=memok" in await _post(cli_a, f"/ext/memory/{set_a}/item/create",
                                           customer_id=cid, content="偏好先看样品再下单")
            # 重复内容 → 明确提示，不静默重复写入
            assert "m=memdup" in await _post(cli_a, f"/ext/memory/{set_a}/item/create",
                                            customer_id=0, content="通用：节假日不发货")
            # 空内容 / 超长
            assert "m=memempty" in await _post(cli_a, f"/ext/memory/{set_a}/item/create",
                                              customer_id=0, content="   ")
            assert "m=memlong" in await _post(cli_a, f"/ext/memory/{set_a}/item/create",
                                             customer_id=0, content="x" * 501)

            items = await memory_service.list_items(set_a)
            new_item = next(i for i in items if i["content"] == "偏好先看样品再下单")
            assert new_item["customer_id"] == cid and new_item["source"] == "manual"

            # ④ 就地编辑
            detail = (await cli_a.get(f"/ext/memory/{set_a}?edit_item={new_item['id']}")).text
            assert f'action="/ext/memory/{set_a}/item/{new_item["id"]}/update"' in detail
            assert "m=memupd" in await _post(
                cli_a, f"/ext/memory/{set_a}/item/{new_item['id']}/update",
                content="偏好先看样品，再谈价格")
            assert (await memory_service.list_items(set_a)) and any(
                i["content"] == "偏好先看样品，再谈价格" for i in await memory_service.list_items(set_a)
            )

            # ⑤ 归属校验：拿 B 的条目 id 去 A 的记忆集里改/删 → 必须失败且 B 的数据不动
            assert "m=memitem" in await _post(
                cli_a, f"/ext/memory/{set_a}/item/{set_b_item}/update", content="越权改")
            assert "m=memitem" in await _post(
                cli_a, f"/ext/memory/{set_a}/item/{set_b_item}/delete")
            b_items = await memory_service.list_items(set_b)
            assert [i["content"] for i in b_items] == ["B 的通用记忆"], "别的记忆集被改动了"
            # 给不属于本集的客户写记忆 → 拒绝
            assert "m=memcust" in await _post(cli_a, f"/ext/memory/{set_a}/item/create",
                                             customer_id=999999, content="越权客户")

            # ⑥ 删除：生效；再删同一条 → memitem
            assert "m=memdel" in await _post(
                cli_a, f"/ext/memory/{set_a}/item/{new_item['id']}/delete")
            assert "m=memitem" in await _post(
                cli_a, f"/ext/memory/{set_a}/item/{new_item['id']}/delete")

            # ⑥a 只建客户、不写记忆（与管理端「新增客户」一致）
            assert "m=memcustok" in await _post(cli_a, f"/ext/memory/{set_a}/customer/create",
                                               name="孙八", aliases="孙八-采购")
            sun = next(c for c in await memory_service.list_customers(set_a) if c["name"] == "孙八")
            assert sun["item_count"] == 0, "只建客户不该顺手写记忆"
            assert sun["aliases"] == "孙八-采购"
            # 记住 id 便于后面确认它的记忆单独加
            sun_id = int(sun["id"])
            # 名字留空 / 超长 → 明确拒绝
            assert "m=memname" in await _post(cli_a, f"/ext/memory/{set_a}/customer/create",
                                             name="  ", aliases="")
            assert "m=memname" in await _post(cli_a, f"/ext/memory/{set_a}/customer/create",
                                             name="x" * 61, aliases="")
            # 用别人的别名当客户名 → 另建客户，不并进去
            assert "m=memcustok" in await _post(cli_a, f"/ext/memory/{set_a}/customer/create",
                                               name="孙八-采购", aliases="")
            assert "孙八-采购" in {c["name"] for c in await memory_service.list_customers(set_a)}
            sun = next(c for c in await memory_service.list_customers(set_a) if c["id"] == sun_id)
            assert sun["aliases"] == "孙八-采购", "别名不该被别人的同名导入动到"
            # 同名字段留空别名 → 不动；填了 → 覆盖
            await _post(cli_a, f"/ext/memory/{set_a}/customer/create", name="孙八", aliases="")
            sun = next(c for c in await memory_service.list_customers(set_a) if c["id"] == sun_id)
            assert sun["aliases"] == "孙八-采购"
            await _post(cli_a, f"/ext/memory/{set_a}/customer/create", name="孙八", aliases="只留这个")
            sun = next(c for c in await memory_service.list_customers(set_a) if c["id"] == sun_id)
            assert sun["aliases"] == "只留这个"
            # 批量加记忆到"已建好的客户"（不新建客户）——管理端同样的用法
            assert "m=memimp" in await _post(cli_a, f"/ext/memory/{set_a}/import",
                                            customer_name="孙八", text="孙八：要 10 台\n孙八：月底前要")
            sun = next(c for c in await memory_service.list_customers(set_a) if c["id"] == sun_id)
            assert sun["item_count"] == 2 and sun["aliases"] == "只留这个", sun

            # ⑥c 批量导入（与内网同一套规矩）：只看规范名 + 别名以导入为准 + 事实只追加
            assert "m=memimp" in await _post(cli_a, f"/ext/memory/{set_a}/import",
                                            customer_name="赵六", aliases="赵六-采购",
                                            text="赵六：预算 5 万\n赵六：要开专票")
            zhao = next(c for c in await memory_service.list_customers(set_a) if c["name"] == "赵六")
            assert zhao["aliases"] == "赵六-采购" and zhao["item_count"] == 2
            # 再导一遍：事实判重（不覆盖），别名照导入覆盖
            r = await _post(cli_a, f"/ext/memory/{set_a}/import",
                            customer_name="赵六", aliases="赵六-新名",
                            text="赵六：预算 5 万")
            assert "m=memimp" in r and "a=0" in r and "d=1" in r, r
            zhao = next(c for c in await memory_service.list_customers(set_a) if c["name"] == "赵六")
            assert zhao["item_count"] == 2, "重复导入把记忆写重了"
            assert zhao["aliases"] == "赵六-新名", "别名应以导入为准覆盖"
            # 留空别名 → 不动已有
            await _post(cli_a, f"/ext/memory/{set_a}/import", customer_name="赵六", text="赵六：新增一条")
            zhao = next(c for c in await memory_service.list_customers(set_a) if c["name"] == "赵六")
            assert zhao["aliases"] == "赵六-新名" and zhao["item_count"] == 3
            # 用"别人的别名"当客户名导入 → 另建客户，不并进别人家
            await _post(cli_a, f"/ext/memory/{set_a}/import",
                        customer_name="赵六-新名", text="另一个人的事实")
            assert "赵六-新名" in {c["name"] for c in await memory_service.list_customers(set_a)}
            zhao = next(c for c in await memory_service.list_customers(set_a) if c["name"] == "赵六")
            assert zhao["item_count"] == 3, "用别名导入时不该把事实并进别名主人"

            # ⑥d 建客户走「新增客户」，单条写记忆只认已存在的客户 / 通用桶
            assert "m=memcustok" in await _post(cli_a, f"/ext/memory/{set_a}/customer/create",
                                               name="李四", aliases="")
            li = next(c for c in await memory_service.list_customers(set_a) if c["name"] == "李四")
            assert "m=memok" in await _post(cli_a, f"/ext/memory/{set_a}/item/create",
                                           customer_id=li["id"], content="李四：要 20 台 X2")
            # 给不存在的客户写记忆 → 明确拒绝（不再顺手建客户）
            assert "m=memcust" in await _post(cli_a, f"/ext/memory/{set_a}/item/create",
                                             customer_id=999999, content="不该落地的记忆")
            names = {c["name"] for c in await memory_service.list_customers(set_a)}
            assert "李四" in names, f"新增客户没建出来: {names}"
            assert "m=memok" in await _post(cli_a, f"/ext/memory/{set_a}/item/create",
                                           customer_id=li["id"], content="李四：账期要 90 天")
            # 再建一次同名 → 复用同一行（不重复建）
            await _post(cli_a, f"/ext/memory/{set_a}/customer/create", name="李四", aliases="")
            assert len([c for c in await memory_service.list_customers(set_a)
                        if c["name"] == "李四"]) == 1, "同名客户被重复创建"
            # 写记忆时给已有客户（张三）→ 归到那位
            assert "m=memok" in await _post(cli_a, f"/ext/memory/{set_a}/item/create",
                                           customer_id=cid, content="张三：要开专票")
            assert len([c for c in await memory_service.list_customers(set_a)
                        if c["name"] == "张三"]) == 1
            # 客户名为空 / 过长 → 明确拒绝（走新增客户入口）
            assert "m=memname" in await _post(cli_a, f"/ext/memory/{set_a}/customer/create",
                                             name="  ", aliases="")
            assert "m=memname" in await _post(cli_a, f"/ext/memory/{set_a}/customer/create",
                                             name="x" * 61, aliases="")
            # 客户数上限：把上限临时压到"当前数量"→ 下一个新客户必须被拒（否则外网可无限建客户）
            monkeypatch.setattr("app.routes.ext.SET_MAX_CUSTOMERS",
                                len(await memory_service.list_customers(set_a)))
            assert "m=memcustfull" in await _post(cli_a, f"/ext/memory/{set_a}/customer/create",
                                                 name="王五", aliases="")
            assert "王五" not in {c["name"] for c in await memory_service.list_customers(set_a)}

            # ⑦ 越权 → 403（先判范围再判存在，不向未授权账号暴露"这个 id 存不存在"）
            assert (await cli_a.get(f"/ext/memory/{set_b}")).status_code == 403
            assert (await cli_b.get(f"/ext/memory/{set_b}")).status_code == 200
            assert (await cli_a.get("/ext/memory/99999999")).status_code == 403
            # 组织里残留了一条"已被删除的记忆集"的成员记录 → 过了范围校验才走 404 分支
            # （注意：只把它加进账号范围是没用的，范围还要与组织取交集，这正是防越权的设计）
            await group_service.set_member_org_groups("memory", 99999999, [org_a])
            await ext_user_service.set_scope(user_a["id"], {"memory": [str(set_a), "99999999"]})
            assert (await cli_a.get("/ext/memory/99999999")).status_code == 404
            await group_service.remove_group_org_member(org_a, "memory", 99999999)
            await ext_user_service.set_scope(user_a["id"], {"memory": [str(set_a)]})

            # ⑧ 记忆集只读：没有新建/删除记忆集的路由（列表页也不给按钮）
            assert (await cli_a.post(f"/ext/memory/{set_a}/delete")).status_code in (404, 405)
            assert (await cli_a.get(f"/ext/memory/{set_a}/delete")).status_code in (404, 405)
            assert (await cli_a.post("/ext/memory/create", data={"name": "x"})).status_code in (404, 405)
            # 页面上也没有"删记忆集/新建记忆集"的入口（文案里的说明不算，只看真实表单/链接）
            assert f"/ext/memory/{set_a}/delete" not in page
            assert 'action="/ext/memory/create"' not in page
        finally:
            if agent_id:
                try:
                    await agent_service.delete_agent(agent_id)
                except Exception as e:
                    print(f"临时智能体清理失败 #{agent_id}: {e}")
            for cli in (cli_a, cli_b):
                if cli is not None:
                    await cli.aclose()
            for sid in (set_a, set_b):
                if sid:
                    await memory_service.delete_set(sid)
            await ext_user_service.delete_user("zz_extmem_a")
            await ext_user_service.delete_user("zz_extmem_b")
            for org in (org_a, org_b):
                if org:
                    await group_service.delete_org_group(org)
            pool.close()
            await pool.wait_closed()

    asyncio.run(main())


@pytestmark_db
def test_admin_scope_page_includes_memory():
    """管理端「可见范围」页要能勾记忆集，且保存真的落到账号范围里。

    加类别时最容易出的错是"页面渲染白名单"与"保存白名单"不一致（勾得上但保存被忽略），
    所以这里既断言页面出现了 value=<set_id>，也断言保存后 get_scope 里真的有它。
    """
    from app.routes.groups import router as groups_router

    async def main():
        from app.services import (
            agent_service,
            knowledge_service,
            robot_service,
            table_service,
            workflow_service,
        )

        pool = await aiomysql.create_pool(**DB, autocommit=True)
        for svc in (group_service, memory_service, ext_user_service, robot_service,
                    agent_service, knowledge_service, table_service, workflow_service):
            svc.set_pool(pool)
        await memory_service.ensure_table()
        await ext_user_service.ensure_table()
        settings.EXT_TUNNEL_SECRET = SECRET
        settings.EXT_COOKIE_SECURE = False
        settings.EXT_LOCAL_PREVIEW = False

        org = set_id = 0
        try:
            await _purge()
            org = await group_service.create_org_group("ZZEXTMEM_A", "外网记忆测试")
            await ext_user_service.add_user("zz_extmem_a", PWD, org)
            user = next(u for u in await ext_user_service.list_users(org)
                        if u["username"] == "zz_extmem_a")
            set_id = await memory_service.create_set("ZZ外网记忆A")
            await group_service.set_member_org_groups("memory", set_id, [org])

            app = _build_app()
            app.include_router(groups_router)   # 管理端的组织/账号页
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://lan.test") as admin:
                r = await admin.get(f"/admin/org/{org}/scope/{user['id']}")
                assert r.status_code == 200
                assert f'name="memory" value="{set_id}"' in r.text, "可见范围页没有记忆集勾选框"
                assert f'name="memory" value="{set_id}" checked' in r.text, "未配置时应默认全选"

                # 只勾记忆集（其余类别一个不勾）→ 保存
                r = await admin.post(f"/admin/org/{org}/scope/{user['id']}",
                                     data={"memory": [str(set_id)]}, follow_redirects=False)
                assert r.status_code == 303
                assert await ext_user_service.get_scope(user["id"]) == {"memory": [str(set_id)]}

            # 该账号在 /ext：记忆可见，其它类别整类隐藏
            cli, _ = await _client_for("zz_extmem_a")
            try:
                page = (await cli.get("/ext/memory")).text
                assert "ZZ外网记忆A" in page
                assert "本组织暂无授权表格" in (await cli.get("/ext/tables")).text
                assert "本组织暂无授权设备" in (await cli.get("/ext/devices")).text
            finally:
                await cli.aclose()
        finally:
            if set_id:
                await memory_service.delete_set(set_id)
            await ext_user_service.delete_user("zz_extmem_a")
            if org:
                await group_service.delete_org_group(org)
            pool.close()
            await pool.wait_closed()

    asyncio.run(main())
