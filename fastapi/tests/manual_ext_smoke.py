"""外网入口（/ext）在真实数据库上的端到端冒烟 —— 手工运行，pytest 不会收集。

    # 只读校验（默认，安全，不动任何设备状态）
    venv/bin/python tests/manual_ext_smoke.py

    # 额外验证"推送开关"的写入路径（会短暂改一台设备的开关并立刻还原）
    venv/bin/python tests/manual_ext_smoke.py --allow-write

为什么不用 uvicorn 起真服务来测：main.py 的 startup 会执行
`robot_service.reset_all_online()`，在生产的服务还活着时再起一个实例，
会把在线设备在库里标成离线 → 设备误报离线。这里自建一个最小 ASGI app
（只挂 ext 路由 + 中间件），只注入连接池，不触发任何 startup 副作用。

为什么不用 fastapi.testclient：它把 app 跑在另一个事件循环里，aiomysql 连接池
（绑定在 asyncio.run 的循环上）无法跨循环使用 → "attached to a different loop"。
httpx.ASGITransport 与连接池同循环，规避该问题。

脚本会临时创建两个组织（SMOKE_A / SMOKE_B）和两个账号，结束时全部清理。
"""

import argparse
import asyncio
import secrets
import sys
import time
from pathlib import Path

import aiomysql
import httpx
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.models.table import TableCreateRequest  # noqa: E402
from app.routes.ext import (  # noqa: E402
    ext_session_middleware,
    ext_tunnel_guard,
    router as ext_router,
)
from app.routes.groups import router as groups_router  # noqa: E402
from app.services import (  # noqa: E402
    agent_service,
    group_service,
    knowledge_service,
    memory_service,
    robot_service,
    table_service,
    task_service,
    workflow_service,
)
from app.services.ext_user_service import (  # noqa: E402
    ext_user_service,
    make_session,
    parse_session,
    session_key,
    session_ttl,
)

SECRET = "smoke-secret-" + secrets.token_hex(4)
HDR = {"X-Tunnel-Secret": SECRET}
PASSED, FAILED = [], []


def check(name: str, ok: bool, extra: str = ""):
    (PASSED if ok else FAILED).append(name)
    print(f"  {'✅' if ok else '❌'} {name}{(' — ' + str(extra)) if extra and not ok else ''}")


def build_app() -> FastAPI:
    reset_flags()
    app = FastAPI()
    # 与 main.py 相同的中间件组合与顺序（后注册者在外层）
    app.middleware("http")(ext_session_middleware)
    app.middleware("http")(ext_tunnel_guard)
    app.include_router(ext_router)
    app.include_router(groups_router)   # 组织成员 / 外网账号管理页
    return app


def reset_flags():
    """脚本自己控制开关，避免被本地 .env（如 EXT_LOCAL_PREVIEW=true）影响。"""
    settings.EXT_TUNNEL_SECRET = SECRET
    settings.EXT_COOKIE_SECURE = False
    settings.EXT_LOCAL_PREVIEW = False


async def main(allow_write: bool) -> int:
    pool = await aiomysql.create_pool(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_DATABASE,
        minsize=1,
        maxsize=3,
        autocommit=True,
        charset="utf8mb4",
    )
    for svc in (
        group_service,
        robot_service,
        agent_service,
        knowledge_service,
        memory_service,      # /ext/memory 页要用
        table_service,
        workflow_service,
        task_service,
    ):
        svc.set_pool(pool)
    ext_user_service.set_pool(pool)
    await ext_user_service.ensure_table()

    transport = httpx.ASGITransport(app=build_app())
    org_a = org_b = 0
    tmp_tbl = 0
    users = []
    memberships = []
    try:
        # ── 造两个组织：A 有授权、B 什么都没有 ──
        org_a = await group_service.create_org_group("SMOKE_A", "冒烟临时组织")
        org_b = await group_service.create_org_group("SMOKE_B", "冒烟临时组织")
        print(f"临时组织: A={org_a} B={org_b}")

        tables = await table_service.list_tables()
        agents = await agent_service.list_agents()
        kbs = await knowledge_service.list_kbs()
        robots = await robot_service.list_robots()
        print(
            f"现有数据: 表格 {len(tables)} / 智能体 {len(agents)} / 知识库 {len(kbs)} / 设备 {len(robots)}"
        )

        first = {}
        if tables:
            first["table"] = str(tables[0].id)
        if agents:
            first["agent"] = str(agents[0].id)
        if kbs:
            first["knowledge"] = str(kbs[0].id)
        if robots:
            first["device"] = str(robots[0].robot_id)
        for kind, mid in first.items():
            await group_service.add_group_org_member(org_a, kind, mid)
            memberships.append((org_a, kind, mid))
        print(f"已授权给 A: {first or '（库为空，无资源可授权）'}")

        pwd_a, pwd_b = "pw-" + secrets.token_hex(4), "pw-" + secrets.token_hex(4)
        await ext_user_service.add_user("smoke_a", pwd_a, org_a)
        await ext_user_service.add_user("smoke_b", pwd_b, org_b)
        users = [("smoke_a", org_a), ("smoke_b", org_b)]

        async with httpx.AsyncClient(transport=transport, base_url="http://ext.test") as anon:
            # ── 门 1 ──
            print("\n[门 1] 隧道密钥头")
            check("无密钥头 → 403", (await anon.get("/ext/ping")).status_code == 403)
            check(
                "错误密钥 → 403",
                (await anon.get("/ext/ping", headers={"X-Tunnel-Secret": "bad"})).status_code == 403,
            )
            check("正确密钥 → 200", (await anon.get("/ext/ping", headers=HDR)).status_code == 200)

        async with httpx.AsyncClient(transport=transport, base_url="http://ext.test") as c:
            # ── 门 2 ──
            print("\n[门 2] 登录会话")
            r = await c.get("/ext/", headers=HDR)
            check("未登录访问首页 → 跳登录", r.status_code == 303, r.status_code)
            r = await c.post("/ext/login", headers=HDR, data={"username": "smoke_a", "password": "wrong"})
            check("错误密码 → 401", r.status_code == 401, r.status_code)
            r = await c.post("/ext/login", headers=HDR, data={"username": "smoke_a", "password": pwd_a})
            check(
                "正确密码 → 303 且下发会话",
                r.status_code == 303 and "ext_session" in c.cookies,
                r.status_code,
            )

            # ── 会话有效期与惰性续期 ──
            print("\n[会话] 有效期与活动续期")
            auth = await ext_user_service.authenticate("smoke_a", pwd_a)
            key = session_key(SECRET)
            tr3 = httpx.ASGITransport(app=transport.app, client=("192.168.31.97", 5555))
            async with httpx.AsyncClient(transport=tr3, base_url="http://ext.test") as cr:
                # 接近到期（只剩 1 小时）→ 应当被续期
                cr.cookies.clear()
                cr.cookies.set(
                    "ext_session",
                    make_session(auth["id"], auth["org_id"], key, fp=auth["pwd_fp"], ttl=3600),
                )
                r = await cr.get("/ext/", headers=HDR)
                setc = r.headers.get("set-cookie") or ""
                check("接近到期的会话被自动续期（响应带新 cookie）",
                      r.status_code == 200 and "ext_session=" in setc, str(r.status_code))
                # 从响应头取新 cookie（jar 里可能有同名的旧条目，别去那儿拿）
                token = setc.split("ext_session=", 1)[1].split(";", 1)[0] if "ext_session=" in setc else ""
                fresh = parse_session(token, key) or {}
                left_h = (int(fresh.get("e", 0)) - int(time.time())) / 3600
                check(f"续期后重新满 {session_ttl() // 3600} 小时", left_h > session_ttl() / 3600 - 1,
                      f"剩余 {left_h:.1f} 小时")
                check("续期后的会话仍然有效", (await cr.get("/ext/", headers=HDR)).status_code == 200)

                # 刚签发的会话不该被无谓重发 cookie
                cr.cookies.clear()
                cr.cookies.set(
                    "ext_session",
                    make_session(auth["id"], auth["org_id"], key, fp=auth["pwd_fp"]),
                )
                r2 = await cr.get("/ext/", headers=HDR)
                check("新会话不会重复下发 cookie（不浪费带宽）", r2.headers.get("set-cookie") is None)

            print("\n[页面] A 账号各页面 200")
            if robots:
                home_html = (await c.get("/ext/", headers=HDR)).text
                check(
                    "首页有 LLM 推送快捷关闭模块",
                    "LLM 推送快捷关闭" in home_html and "/ext/auto_reply_all" in home_html,
                )
            for path in ("/ext/", "/ext/tables", "/ext/devices", "/ext/agents", "/ext/knowledge", "/ext/memory"):
                check(f"{path} → 200", (await c.get(path, headers=HDR)).status_code == 200)

            # ── 门 3：组织隔离 ──
            print("\n[门 3] 组织隔离（B 无任何授权）")
            if tables:
                t0 = tables[0]
                check("A 能看到自己的表格", t0.name in (await c.get("/ext/tables", headers=HDR)).text)
                # 下载：全量 / 筛选后 / 非法字段三条路径
                r = await c.get(f"/ext/table/{t0.table_key}/export", headers=HDR)
                check(
                    "全量下载 → CSV 附件（带 BOM）",
                    r.status_code == 200
                    and r.text.startswith("\ufeff")
                    and r.headers.get("content-type", "").startswith("text/csv")
                    and "attachment" in r.headers.get("content-disposition", ""),
                    r.status_code,
                )
                detail = (await c.get(f"/ext/table/{t0.table_key}", headers=HDR)).text
                check("详情页有筛选后下载按钮", f'formaction="/ext/table/{t0.table_key}/export"' in detail)
                check("详情页有比较方式选择", 'name="f0_op"' in detail and "包含" in detail)
                check(
                    "表格列表页有下载入口",
                    f'href="/ext/table/{t0.table_key}/export"'
                    in (await c.get("/ext/tables", headers=HDR)).text,
                )
                tinfo = await table_service.get_table_by_key(t0.table_key)
                f0 = (tinfo or {}).get("fields", [{}])[0]
                fld, ftype = f0.get("name", ""), f0.get("type", "")
                # 按字段类型挑一个「必然查不到」的条件，验证筛选真的进了 SQL
                no_match = {
                    "int": {"op": ">", "value": "999999999999"},
                    "float": {"op": ">", "value": "999999999999"},
                    "datetime": {"op": ">", "value": "2999-01-01"},
                }.get(ftype, {"op": "like", "value": "___no_such_value___"})
                if fld:
                    r = await c.get(
                        f"/ext/table/{t0.table_key}/export",
                        headers=HDR,
                        params={"field": fld, **no_match},
                    )
                    check(
                        f"筛选后下载（{ftype} {no_match['op']}）→ 只剩表头",
                        r.status_code == 200 and r.text.count("\n") == 1,
                        r.status_code,
                    )
                    check(
                        "页面查询同样条件 → 0 行",
                        "没有符合条件的数据"
                        in (
                            await c.get(
                                f"/ext/table/{t0.table_key}",
                                headers=HDR,
                                params={"field": fld, **no_match},
                            )
                        ).text,
                    )
                if fld and ftype in ("int", "float"):
                    r = await c.get(
                        f"/ext/table/{t0.table_key}/export?field={fld}&op=like&value=x", headers=HDR
                    )
                    check("数字列上用「包含」→ 400（类型不匹配不猜）", r.status_code == 400, r.status_code)
                r = await c.get(f"/ext/table/{t0.table_key}/export?field=__nope__", headers=HDR)
                check("非法筛选字段 → 400", r.status_code == 400, r.status_code)

            # ── 表格筛选：按字段类型给不同运算符（临时表，用完即删）──
            print("\n[表格筛选] 字段类型 × 运算符 × 下载")
            tmp_tbl = await table_service.create_table(
                TableCreateRequest(
                    name="SMOKE_TYPES_" + secrets.token_hex(3),
                    description="冒烟临时表",
                    fields=[
                        {"name": "s", "type": "varchar", "label": "文本"},
                        {"name": "n", "type": "int", "label": "整数"},
                        {"name": "f", "type": "float", "label": "小数"},
                        {"name": "d", "type": "datetime", "label": "时间"},
                        {"name": "t", "type": "text", "label": "长文本"},
                        {"name": "j", "type": "json", "label": "JSON"},
                    ],
                )
            )
            tkey = f"tbl_{tmp_tbl}"
            await group_service.add_group_org_member(org_a, "table", str(tmp_tbl))
            for row in (
                {"s": "apple", "n": 1, "f": 1.5, "d": "2024-03-05 08:00:00",
                 "t": "长文本甲", "j": {"k": "a"}},
                {"s": "banana", "n": 5, "f": 2.5, "d": "2024-03-06 09:30:00",
                 "t": "长文本乙", "j": {"k": "b"}},
                {"s": "cherry", "n": 10, "f": 10.25, "d": "2024-04-01 00:00:00",
                 "t": "别的东西", "j": {"k": "c"}},
            ):
                await table_service.insert_row(tkey, row)

            async def matched(**params) -> int:
                """走下载接口数行数：比解析 HTML 更直接，也顺带验证下载复用筛选。"""
                r = await c.get(f"/ext/table/{tkey}/export", headers=HDR, params=params)
                return r.text.count("\n") - 1 if r.status_code == 200 else -1

            check("不筛选 → 3 行", await matched() == 3)
            check("文本 包含 an → 1 行",
                  await matched(field="s", op="like", value="an") == 1)
            check("整数 大于 4 → 2 行",
                  await matched(field="n", op=">", value="4") == 2)
            check("整数 等于 5 → 1 行",
                  await matched(field="n", op="=", value="5") == 1)
            check("小数 不大于 2.5 → 2 行",
                  await matched(field="f", op="<=", value="2.5") == 2)
            check("时间 等于某天 → 当天那一行（不是只有 00:00:00 才中）",
                  await matched(field="d", op="=", value="2024-03-05") == 1)
            check("时间 晚于某天 → 次日 0 点起，共 2 行",
                  await matched(field="d", op=">", value="2024-03-05") == 2)
            check("时间 不晚于某天 → 当天结束前，共 1 行",
                  await matched(field="d", op="<=", value="2024-03-05") == 1)
            check("时间 不早于某天 → 当天 0 点起，共 3 行",
                  await matched(field="d", op=">=", value="2024-03-05") == 3)
            check("长文本 包含 → 2 行",
                  await matched(field="t", op="like", value="长文本") == 2)
            check("JSON 包含 → 1 行",
                  await matched(field="j", op="like", value='"k": "b"') == 1)
            # 多条件：同一字段两条 = 区间；不同字段两条 = 并且
            check("同字段两条 → 区间 1 < n < 10，共 1 行",
                  await matched(n=2, f0_field="n", f0_op=">", f0_value="1",
                                f1_field="n", f1_op="<", f1_value="10") == 1)
            check("跨字段两条 → s 含 an 且 n > 1，共 1 行",
                  await matched(n=2, f0_field="s", f0_op="like", f0_value="an",
                                f1_field="n", f1_op=">", f1_value="1") == 1)
            check("条件互斥 → 0 行",
                  await matched(n=2, f0_field="s", f0_op="like", f0_value="an",
                                f1_field="n", f1_op="=", f1_value="1") == 0)
            check("空条件行被忽略（不筛选 → 3 行）",
                  await matched(n=2, f0_field="", f0_op="", f0_value="",
                                f1_field="", f1_op="", f1_value="") == 3)
            page = (await c.get(f"/ext/table/{tkey}", headers=HDR,
                                params={"n": 1, "f0_field": "n"})).text
            check("页面提示了时间/多条件的用法", "整天" in page and "并且" in page and "2026-08-09" in page)
            check("＋添加条件 → 多渲染一行",
                  'name="f1_field"' in (
                      await c.get(f"/ext/table/{tkey}", headers=HDR,
                                  params={"n": 1, "f0_field": "n", "add": "1"})
                  ).text)
            check("✕ 删除条件 → 少一行",
                  'name="f1_field"' not in (
                      await c.get(f"/ext/table/{tkey}", headers=HDR,
                                  params={"n": 2, "f0_field": "n", "f1_field": "s", "del": "1"})
                  ).text)
            page_n = (await c.get(f"/ext/table/{tkey}", headers=HDR, params={"field": "n"})).text
            check("选整数列 → 比较方式只有数字类",
                  '<option value=">"' in page_n and '<option value="like"' not in page_n)
            page_s = (await c.get(f"/ext/table/{tkey}", headers=HDR, params={"field": "s"})).text
            check("选文本列 → 比较方式有「包含」、没有大于小于",
                  '<option value="like"' in page_s and '<option value=">"' not in page_s)

            async with httpx.AsyncClient(transport=transport, base_url="http://ext.test") as cb:
                await cb.post("/ext/login", headers=HDR, data={"username": "smoke_b", "password": pwd_b})
                body_b = (await cb.get("/ext/tables", headers=HDR)).text + (
                    await cb.get("/ext/devices", headers=HDR)
                ).text
                if tables:
                    check("B 看不到 A 的表格", tables[0].name not in body_b, tables[0].name)
                    r = await cb.get(f"/ext/table/{tables[0].table_key}", headers=HDR)
                    check("B 直接访问 A 的表格 → 403", r.status_code == 403, r.status_code)
                    r = await cb.get(f"/ext/table/{tables[0].table_key}/export", headers=HDR)
                    check("B 下载 A 的表格 → 403", r.status_code == 403, r.status_code)
                if robots:
                    r = await cb.post(
                        f"/ext/device/{robots[0].robot_id}/auto_reply", headers=HDR, data={"enabled": "0"}
                    )
                    check("B 改 A 的设备开关 → 403", r.status_code == 403, r.status_code)
                if kbs:
                    # 检索入口已按需求整体移除：对谁都应该是 404
                    r = await cb.post(f"/ext/knowledge/{kbs[0].id}/test", headers=HDR, data={"query": "x"})
                    check("知识库检索端点已移除（404）", r.status_code == 404, r.status_code)
                if agents:
                    r = await cb.post(f"/ext/agent/{agents[0].id}", headers=HDR, data={"provider": "x"})
                    check("B 改 A 的智能体 → 403", r.status_code == 403, r.status_code)

            # ── 写路径 ──
            print("\n[写路径]")
            if agents:
                a0 = agents[0]
                r = await c.post(
                    f"/ext/agent/{a0.id}",
                    headers=HDR,
                    data={
                        "provider": a0.provider,
                        "model": a0.model or "smoke-model",
                        "base_url": a0.base_url,
                        "temperature": str(a0.temperature),
                        "max_tokens": str(a0.max_tokens),
                        "enabled": "1",
                        "description": a0.description,
                        "system_prompt": a0.system_prompt,
                    },
                )
                back = await agent_service.get_agent(a0.id)
                check(
                    "A 改模型配置成功且 Key 未被清空",
                    r.status_code == 303
                    and (back.model or "") == (a0.model or "smoke-model")
                    and bool(back.api_key) == bool(a0.api_key),
                    r.status_code,
                )
                check(
                    "页面不泄露 API Key",
                    not a0.api_key or a0.api_key not in (await c.get(f"/ext/agent/{a0.id}", headers=HDR)).text,
                )

            if robots and allow_write:
                # 批量关闭：先把设备打开，再一键全关，验证后还原
                rid = robots[0].robot_id
                origin = (await robot_service.get_robot(rid)).auto_reply
                await robot_service.update_auto_reply(rid, True)
                r = await c.post(
                    "/ext/auto_reply_all", headers=HDR, data={"enabled": "0", "next": "home"}
                )
                off_now = not (await robot_service.get_robot(rid)).auto_reply
                await robot_service.update_auto_reply(rid, origin)
                check(
                    "首页一键全部关闭生效且已还原",
                    r.status_code == 303 and off_now
                    and (await robot_service.get_robot(rid)).auto_reply == origin,
                    f"origin={origin} off={off_now}",
                )

            if robots and allow_write:
                rid = robots[0].robot_id
                before = (await robot_service.get_robot(rid)).auto_reply
                await c.post(
                    f"/ext/device/{rid}/auto_reply",
                    headers=HDR,
                    data={"enabled": "0" if before else "1"},
                )
                mid = (await robot_service.get_robot(rid)).auto_reply
                await robot_service.update_auto_reply(rid, before)  # 立刻还原
                after = (await robot_service.get_robot(rid)).auto_reply
                check("开关写入生效且已还原", mid != before and after == before, f"{before}→{mid}→{after}")
            elif robots:
                print("  ⏭  跳过开关写入（未加 --allow-write）；越权写入已由 403 用例覆盖")

            # ── 控制台收敛：只读项与移除的功能 ──
            print("\n[顶栏] 账号菜单")
            hdr = (await c.get("/ext/", headers=HDR)).text
            menu_ok = (
                "<details class=\"x-menu\">" in hdr
                and "<summary" in hdr
                and 'href="/ext/password"' in hdr
                and 'href="/ext/logout"' in hdr
                and "Escape" in hdr  # 点外部/Esc 收起的脚本
            )
            check("账号菜单用 details（触摸可点）+ 含改密/退出 + 收起脚本", menu_ok)

            print("\n[收敛] 只读与已移除入口")
            home_html = (await c.get("/ext/", headers=HDR)).text
            check(
                "首页快捷关闭在统计卡之前（置顶）",
                home_html.find("LLM 推送快捷关闭") < home_html.find("x-stats")
                if "x-stats" in home_html else False,
            )
            dev_html = (await c.get("/ext/devices", headers=HDR)).text
            check("设备页无绑定智能体下拉", 'name="agent_id"' not in dev_html)
            if robots:
                r = await c.post(
                    f"/ext/device/{robots[0].robot_id}/agent", headers=HDR, data={"agent_id": "1"}
                )
                check("改绑端点已移除（404）", r.status_code == 404, r.status_code)

            if agents:
                a0 = agents[0]
                edit_html = (await c.get(f"/ext/agent/{a0.id}", headers=HDR)).text
                locked = [n for n in ("temperature", "max_tokens", "description", "system_prompt", "enabled")
                          if f'name="{n}"' in edit_html]
                check("智能体页锁定项不是可输入控件", not locked, str(locked))
                check("智能体页仍可改模型", 'name="model"' in edit_html and 'name="base_url"' in edit_html)
                # 系统提示词属于内网资产，客户控制台连只读展示都不给
                check("智能体页不显示系统提示词", "系统提示词" not in edit_html)
                # 构造请求硬改锁定字段：必须无效（不是"界面上藏起来"）
                await c.post(
                    f"/ext/agent/{a0.id}",
                    headers=HDR,
                    data={"provider": a0.provider, "model": a0.model or "m", "base_url": a0.base_url,
                          "temperature": "1.9", "max_tokens": "12345",
                          "system_prompt": "HACKED", "description": "HACKED", "enabled": "0"},
                )
                back = await agent_service.get_agent(a0.id)
                check(
                    "构造请求也改不动锁定字段",
                    abs(float(back.temperature) - float(a0.temperature)) < 1e-9
                    and int(back.max_tokens) == int(a0.max_tokens)
                    and back.system_prompt == a0.system_prompt
                    and back.description == a0.description
                    and bool(back.enabled) == bool(a0.enabled),
                    f"t={back.temperature} mt={back.max_tokens} sp={'改' if back.system_prompt != a0.system_prompt else '未改'}",
                )

            # ── 知识库：无检索入口 + 详情页 ──
            print("\n[知识库] 无检索入口 + 详情页")
            tmp_kb = await knowledge_service.create_kb(
                "SMOKE_KB", "local", "冒烟临时知识库", {}, 500, 50, 3
            )
            created_kb = False
            try:
                await group_service.add_group_org_member(org_a, "knowledge", str(tmp_kb))
                memberships.append((org_a, "knowledge", str(tmp_kb)))
                created_kb = True
                kb_list = (await c.get("/ext/knowledge", headers=HDR)).text
                check("知识库列表有详情入口", f'href="/ext/knowledge/{tmp_kb}"' in kb_list)
                check("知识库列表无检索输入框", 'name="query"' not in kb_list)
                detail = await c.get(f"/ext/knowledge/{tmp_kb}", headers=HDR)
                check("知识库详情页 → 200", detail.status_code == 200, detail.status_code)
                check(
                    "详情页含元信息与文档管理区",
                    "本地文档库" in detail.text and "文档管理" in detail.text
                    and "切分长度" in detail.text and "该知识库还没有文档" in detail.text
                    and 'enctype="multipart/form-data"' in detail.text,
                )
                check("检索端点已移除（404）",
                      (await c.post(f"/ext/knowledge/{tmp_kb}/test", headers=HDR,
                                    data={"query": "x"})).status_code == 404)

                # 本地库：上传 → 列表出现 → 删除 → 消失
                up = await c.post(
                    f"/ext/knowledge/{tmp_kb}/upload",
                    headers=HDR,
                    files={"file": ("smoke_doc.txt", "冒烟测试文档内容。第二行。".encode(), "text/plain")},
                )
                docs_after = await knowledge_service.list_documents(tmp_kb)
                check("本地库可上传文档并切分",
                      up.status_code == 303 and len(docs_after) == 1 and docs_after[0]["chunk_count"] >= 1,
                      f"{up.status_code} docs={len(docs_after)}")
                detail2 = (await c.get(f"/ext/knowledge/{tmp_kb}", headers=HDR)).text
                check("详情页列出文档并给删除按钮",
                      "smoke_doc.txt" in detail2 and "文档管理" in detail2
                      and f"/ext/knowledge/{tmp_kb}/doc/" in detail2)

                # 扩展名白名单（服务端强校验，不看 accept 属性）
                bad = await c.post(
                    f"/ext/knowledge/{tmp_kb}/upload", headers=HDR,
                    files={"file": ("evil.sh", b"#!/bin/sh echo hi", "text/plain")},
                )
                check("非白名单扩展名被拒",
                      bad.status_code == 303 and len(await knowledge_service.list_documents(tmp_kb)) == 1)

                # 删别的库的文档必须被拒（delete_document 本身不校验归属）
                if docs_after:
                    other = await c.post(f"/ext/knowledge/{tmp_kb}/doc/99999/delete", headers=HDR)
                    check("删除不存在的文档被拒",
                          other.status_code == 303 and len(await knowledge_service.list_documents(tmp_kb)) == 1)

                if docs_after:
                    did = docs_after[0]["id"]
                    ok_del = await c.post(f"/ext/knowledge/{tmp_kb}/doc/{did}/delete", headers=HDR)
                    check("本地库可删除文档",
                          ok_del.status_code == 303
                          and not await knowledge_service.list_documents(tmp_kb))

                async with httpx.AsyncClient(transport=transport, base_url="http://ext.test") as cb2:
                    await cb2.post("/ext/login", headers=HDR, data={"username": "smoke_b", "password": pwd_b})
                    r = await cb2.get(f"/ext/knowledge/{tmp_kb}", headers=HDR)
                    check("B 看 A 的知识库详情 → 403", r.status_code == 403, r.status_code)
                # 云知识库：详情页给说明、上传/删除一律拒绝
                cloud_kb = await knowledge_service.create_kb(
                    "SMOKE_CLOUD_KB", "bailian", "冒烟云库",
                    {"api_key": "sk-should-never-leak", "workspace_id": "ws-1", "collection_name": "kb-1"},
                    500, 50, 3,
                )
                cloud_created = False
                try:
                    await group_service.add_group_org_member(org_a, "knowledge", str(cloud_kb))
                    memberships.append((org_a, "knowledge", str(cloud_kb)))
                    cloud_created = True
                    cdetail = await c.get(f"/ext/knowledge/{cloud_kb}", headers=HDR)
                    check("云知识库详情页说明在平台侧维护",
                          cdetail.status_code == 200 and "平台侧维护" in cdetail.text)
                    check("云知识库详情页无上传表单", "enctype=\"multipart/form-data\"" not in cdetail.text)
                    check("云知识库不泄露 api_key", "sk-should-never-leak" not in cdetail.text)
                    cu = await c.post(
                        f"/ext/knowledge/{cloud_kb}/upload", headers=HDR,
                        files={"file": ("x.txt", b"hi", "text/plain")},
                    )
                    cd = await c.post(f"/ext/knowledge/{cloud_kb}/doc/1/delete", headers=HDR)
                    check("云知识库上传/删除被拒",
                          cu.status_code == 303 and cd.status_code == 303
                          and not await knowledge_service.list_documents(cloud_kb))
                    # 越权：B 账号不能碰 A 的库
                    async with httpx.AsyncClient(transport=transport, base_url="http://ext.test") as cb3:
                        await cb3.post("/ext/login", headers=HDR,
                                       data={"username": "smoke_b", "password": pwd_b})
                        r = await cb3.post(
                            f"/ext/knowledge/{cloud_kb}/upload", headers=HDR,
                            files={"file": ("y.txt", b"hi", "text/plain")},
                        )
                        check("B 往 A 的云库上传 → 403",
                              r.status_code == 403, r.status_code)
                finally:
                    if cloud_created:
                        await knowledge_service.delete_kb(cloud_kb)
            finally:
                if created_kb:
                    await knowledge_service.delete_kb(tmp_kb)

            # ── 客户自助改密码 ──
            print("\n[改密码] 自助修改 + 旧会话失效")
            pw_user = "smoke_pw"
            pw_old = "pw-" + secrets.token_hex(4)
            pw_new = "pw-" + secrets.token_hex(4)
            await ext_user_service.delete_user(pw_user)
            await ext_user_service.add_user(pw_user, pw_old, org_a)
            users.append((pw_user, org_a))

            tr2 = httpx.ASGITransport(app=transport.app, client=("192.168.31.97", 5555))
            async with httpx.AsyncClient(transport=tr2, base_url="http://ext.test") as p1, \
                    httpx.AsyncClient(transport=tr2, base_url="http://ext.test") as p2:
                await p1.post("/ext/login", headers=HDR, data={"username": pw_user, "password": pw_old})
                await p2.post("/ext/login", headers=HDR, data={"username": pw_user, "password": pw_old})
                check("改密页 → 200", (await p1.get("/ext/password", headers=HDR)).status_code == 200)
                check(
                    "当前密码不对 → 401 且密码未变",
                    (await p1.post("/ext/password", headers=HDR, data={
                        "old_password": "wrong-one", "new_password": pw_new, "confirm": pw_new})).status_code == 401
                    and bool(await ext_user_service.authenticate(pw_user, pw_old)),
                )
                check(
                    "新密码过短 → 400",
                    (await p1.post("/ext/password", headers=HDR, data={
                        "old_password": pw_old, "new_password": "short", "confirm": "short"})).status_code == 400,
                )
                check(
                    "两次输入不一致 → 400",
                    (await p1.post("/ext/password", headers=HDR, data={
                        "old_password": pw_old, "new_password": pw_new, "confirm": pw_new + "x"})).status_code == 400,
                )
                r = await p1.post("/ext/password", headers=HDR, data={
                    "old_password": pw_old, "new_password": pw_new, "confirm": pw_new})
                check(
                    "改密成功且新密码可登录、旧密码失效",
                    r.status_code == 303
                    and bool(await ext_user_service.authenticate(pw_user, pw_new))
                    and not await ext_user_service.authenticate(pw_user, pw_old),
                    str(r.status_code),
                )
                check("改密的这台设备仍在线（换了新会话）",
                      (await p1.get("/ext/", headers=HDR)).status_code == 200)
                check("另一台设备上的旧会话被踢下线",
                      (await p2.get("/ext/", headers=HDR, follow_redirects=False)).status_code == 303)
                # 回归：这类"失效会话"访问登录页必须是 200，否则会与首页互相 303 成死循环
                check("失效会话访问登录页 → 渲染登录页（不死循环）",
                      (await p2.get("/ext/login", headers=HDR, follow_redirects=False)).status_code == 200)

            # ── 内网「组织成员 + 外网账号」管理页 ──
            print("\n[内网] 组织成员与账号管理页")
            admin = httpx.ASGITransport(app=transport.app, client=("127.0.0.1", 5555))
            async with httpx.AsyncClient(transport=admin, base_url="http://lan.test") as ca:
                r = await ca.get(f"/admin/org/{org_a}/members")
                check("组织成员页 → 200", r.status_code == 200, r.status_code)
                check("页面含成员与账号两块", "👥 组织成员" in r.text and "🌐 外网客户账号" in r.text)

                if robots:
                    rid = robots[0].robot_id
                    await group_service.remove_group_org_member(org_a, "device", rid)
                    await ca.post(
                        f"/admin/org/{org_a}/members/add",
                        data={"member_type": "device", "member_id": rid},
                    )
                    added = rid in await group_service.get_group_org_members(org_a, "device")
                    check("页面可把资源加入组织", added, f"added={added}")
                    # 移出入口必须彻底不存在：移出组织=一次删除操作，不该在成员页误触
                    gone = await ca.get(
                        f"/admin/org/{org_a}/members/remove?member_type=device&member_id={rid}"
                    )
                    still = rid in await group_service.get_group_org_members(org_a, "device")
                    check("移出入口已移除（404 且成员未变）", gone.status_code == 404 and still,
                          f"{gone.status_code} still={still}")
                    # 成员页不应再出现任何"移除"按钮
                    page_html = (await ca.get(f"/admin/org/{org_a}/members")).text
                    check("成员页无移除按钮", "members/remove" not in page_html)

                # 走页面同一条路径：建号 → 改密 → 停用 → 删除
                ui_user = "smoke_ui"
                ui_pw1, ui_pw2 = "pw-" + secrets.token_hex(4), "pw-" + secrets.token_hex(4)
                await ext_user_service.delete_user(ui_user)
                await ca.post(
                    f"/admin/org/{org_a}/ext_user/add",
                    data={"username": ui_user, "password": ui_pw1},
                )
                created = bool(await ext_user_service.authenticate(ui_user, ui_pw1))
                await ca.post(
                    f"/admin/org/{org_a}/ext_user/passwd",
                    data={"username": ui_user, "password": ui_pw2},
                )
                repw = bool(await ext_user_service.authenticate(ui_user, ui_pw2))
                await ca.get(f"/admin/org/{org_a}/ext_user/toggle?username={ui_user}&on=0")
                disabled = not await ext_user_service.authenticate(ui_user, ui_pw2)
                await ca.get(f"/admin/org/{org_a}/ext_user/delete?username={ui_user}")
                deleted = not await ext_user_service.authenticate(ui_user, ui_pw2)
                check(
                    "页面可建号/改密/停用/删除",
                    created and repw and disabled and deleted,
                    f"create={created} repw={repw} disable={disabled} delete={deleted}",
                )

            # ── 账号级可见范围（组织 = 拥有什么，账号 = 能看其中哪些）──
            print("\n[可见范围] 账号级限定 + 失败关闭")
            if len(agents) >= 2:
                a0, a1 = agents[0], agents[1]
                await group_service.add_group_org_member(org_a, "agent", str(a1.id))
                memberships.append((org_a, "agent", str(a1.id)))

                scope_user = "smoke_scope"
                scope_pw = "pw-" + secrets.token_hex(4)
                await ext_user_service.delete_user(scope_user)
                uid = await ext_user_service.add_user(scope_user, scope_pw, org_a)
                users.append((scope_user, org_a))

                async with httpx.AsyncClient(transport=transport, base_url="http://ext.test") as cs:
                    await cs.post("/ext/login", headers=HDR, data={"username": scope_user, "password": scope_pw})
                    page = (await cs.get("/ext/agents", headers=HDR)).text
                    check("未配范围 → 跟随组织（两个智能体都可见）",
                          a0.name in page and a1.name in page)

                admin2 = httpx.ASGITransport(app=transport.app, client=("127.0.0.1", 5555))
                async with httpx.AsyncClient(transport=admin2, base_url="http://lan.test") as ca2:
                    r = await ca2.get(f"/admin/org/{org_a}/scope/{uid}")
                    check("范围设置页 → 200 且默认全选",
                          r.status_code == 200 and f'value="{a0.id}" checked' in r.text)

                    # 只给 1 个智能体（其余类别一个都不勾）
                    await ca2.post(
                        f"/admin/org/{org_a}/scope/{uid}",
                        data={"agent": [str(a0.id)]},
                    )
                    saved = await ext_user_service.get_scope(uid)
                    check("保存后只写入指定项", saved == {"agent": [str(a0.id)]}, str(saved))

                    # 空保存必须被拒：否则"零记录"会退化成"跟随组织=全可见"
                    await ca2.post(f"/admin/org/{org_a}/scope/{uid}", data={})
                    still = await ext_user_service.get_scope(uid)
                    check("空勾选被拒绝（不会退化成全可见）", still == {"agent": [str(a0.id)]}, str(still))

                async with httpx.AsyncClient(transport=transport, base_url="http://ext.test") as cs:
                    await cs.post("/ext/login", headers=HDR, data={"username": scope_user, "password": scope_pw})
                    page = (await cs.get("/ext/agents", headers=HDR)).text
                    check("限定后：只看到被勾选的智能体",
                          a0.name in page and a1.name not in page)
                    check("限定后：没勾的类别整类看不见（表格）",
                          "本组织暂无授权表格" in (await cs.get("/ext/tables", headers=HDR)).text)
                    check("限定后：没勾的类别整类看不见（设备）",
                          "本组织暂无授权设备" in (await cs.get("/ext/devices", headers=HDR)).text)
                    if tables:
                        r = await cs.get(f"/ext/table/{tables[0].table_key}", headers=HDR)
                        check("限定后：直连未授权的表格 → 403", r.status_code == 403, r.status_code)
                    if robots:
                        rid0 = robots[0].robot_id
                        before_all = (await robot_service.get_robot(rid0)).auto_reply
                        await cs.post(
                            "/ext/auto_reply_all",
                            headers=HDR,
                            data={"enabled": "0", "next": "home"},
                        )
                        after_all = (await robot_service.get_robot(rid0)).auto_reply
                        check(
                            "批量关闭越不过账号范围（未授权的设备不受影响）",
                            after_all == before_all,
                            f"{before_all}→{after_all}",
                        )

            # ── 本地预览模式（EXT_LOCAL_PREVIEW，无云机时看效果用的路径）──
            print("\n[本地预览] 内网直连免密钥头")
            settings.EXT_LOCAL_PREVIEW = True
            lan = httpx.ASGITransport(app=transport.app, client=("192.168.31.97", 5555))
            async with httpx.AsyncClient(transport=lan, base_url="http://lan.test") as cl:
                check("/ext/login 内网直连 → 200", (await cl.get("/ext/login")).status_code == 200)
                r = await cl.post(
                    "/ext/login", data={"username": "smoke_a", "password": pwd_a}
                )
                cookie_hdr = r.headers.get("set-cookie", "")
                check(
                    "登录成功且 cookie 未加 Secure（http 下才存得住）",
                    r.status_code == 303 and "ext_session" in cl.cookies and "Secure" not in cookie_hdr,
                    cookie_hdr[:60],
                )
                check("登录后首页 → 200", (await cl.get("/ext/")).status_code == 200)
            wan = httpx.ASGITransport(app=transport.app, client=("8.8.8.8", 5555))
            async with httpx.AsyncClient(transport=wan, base_url="http://wan.test") as cw:
                check(
                    "公网来源即使开了预览也 → 403",
                    (await cw.get("/ext/login")).status_code == 403,
                )
            settings.EXT_LOCAL_PREVIEW = False

    finally:
        print("\n[清理]")
        if tmp_tbl:
            try:
                await table_service.delete_table(tmp_tbl)
            except Exception as e:
                print(f"  ⚠️ 删除临时表 {tmp_tbl} 失败: {e}")
        for name, _org in users:
            try:
                await ext_user_service.delete_user(name)
            except Exception as e:
                print(f"  ⚠️ 删除账号 {name} 失败: {e}")
        for org_id, kind, mid in memberships:
            try:
                await group_service.remove_group_org_member(org_id, kind, mid)
            except Exception as e:
                print(f"  ⚠️ 移除成员 {kind}/{mid} 失败: {e}")
        for org_id in (org_a, org_b):
            if org_id:
                try:
                    await group_service.delete_org_group(org_id)
                except Exception as e:
                    print(f"  ⚠️ 删除组织 {org_id} 失败: {e}")
        pool.close()
        await pool.wait_closed()
        print("  已清理临时组织与账号")

    print(f"\n{'=' * 50}\n通过 {len(PASSED)} / 失败 {len(FAILED)}")
    for f in FAILED:
        print(f"  ❌ {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--allow-write", action="store_true", help="允许短暂切换一台设备的推送开关（随后还原）"
    )
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.allow_write)))
