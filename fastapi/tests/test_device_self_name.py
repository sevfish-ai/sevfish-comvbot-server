"""设备页「企微名」：手动获取本机在企业微信里的名字（503 我的信息），并显示在设备列表。

背景（真机 2026-10-09）：设备上报的 101 事件不含 at/atList，服务端判断"客户是否 @ 了我"
只能靠正文 + 本机名（`agent_service.mentions_me`）。本机名唯一来源是 503 回执，只存内存，
所以给一个手动获取按钮（**刻意不自动获取**：不打扰设备界面，也避免名字过期导致漏 @）。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiomysql  # noqa: E402

from app.config import settings  # noqa: E402

DB = dict(host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
          password=settings.MYSQL_PASSWORD, db=settings.MYSQL_DATABASE, charset="utf8mb4")
RB = "zz-name-01"


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


async def _pool():
    return await aiomysql.create_pool(**DB, autocommit=True)


@pytestmark_db
def test_device_page_shows_self_name_and_fetch_button():
    """列表要显示企微名（拿到/没拿到两种），并提供「获取名字」按钮。"""
    import httpx

    from app.main import app
    from app.services import (
        agent_service,
        aivbot_service,
        group_service,
        memory_service,
        robot_service,
    )

    async def main():
        pool = await _pool()
        for svc in (robot_service, memory_service, group_service, agent_service):
            svc.set_pool(pool)
        await robot_service.ensure_table()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM robots WHERE robot_id=%s", (RB,))
                    await cur.execute(
                        "INSERT INTO robots (robot_id,token,device_name) VALUES (%s,'t','zz名字设备')",
                        (RB,),
                    )
                await conn.commit()

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as cli:
                html = (await cli.get("/admin")).text
                assert "企微名" in html
                assert f"/admin/robot/{RB}/self_name" in html
                assert "（未获取）" in html          # 缓存里没有 → 占位

                # 设备上报 503 后（缓存里有名字）→ 列表显示名字
                aivbot_service.received_data[RB] = {
                    "my_info": [{"info": {"corporation": "一缆信息科技", "name": "张三"}}]
                }
                html = (await cli.get("/admin")).text
                assert "张三" in html
                assert "用于群聊 @ 判定" in html
        finally:
            aivbot_service.received_data.pop(RB, None)
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM robots WHERE robot_id=%s", (RB,))
                await conn.commit()
            pool.close()
            await pool.wait_closed()

    asyncio.run(main())


@pytestmark_db
def test_fetch_self_name_route(monkeypatch):
    """按钮行为：在线 → 下发 503 并把名字写进缓存；离线 → 明确报错。"""
    import httpx

    from app.main import app
    from app.routes import admin as admin_route
    from app.services import (
        agent_service,
        aivbot_service,
        group_service,
        memory_service,
        robot_service,
    )

    class _FakeWS:
        def __init__(self):
            self.sent = []

        async def send_json(self, msg):
            self.sent.append(msg)

    async def main():
        pool = await _pool()
        for svc in (robot_service, memory_service, group_service, agent_service):
            svc.set_pool(pool)
        await robot_service.ensure_table()
        fake = _FakeWS()
        # 压缩等待，测试不必真等 12 秒
        monkeypatch.setattr(admin_route, "SELF_NAME_POLL", 0.05)
        monkeypatch.setattr(admin_route, "SELF_NAME_TRIES", 4)
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM robots WHERE robot_id=%s", (RB,))
                    await cur.execute(
                        "INSERT INTO robots (robot_id,token,device_name) VALUES (%s,'t','zz名字设备')",
                        (RB,),
                    )
                await conn.commit()

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as cli:
                # ① 离线：没有连接 → 报错、不下发
                r = await cli.post(f"/admin/robot/{RB}/self_name", follow_redirects=True)
                assert "设备离线" in r.text
                assert fake.sent == []

                # ② 在线但无回执 → 提示稍后刷新（不留假名字）
                aivbot_service.active_connections[RB] = [fake]
                r = await cli.post(f"/admin/robot/{RB}/self_name", follow_redirects=True)
                assert "未收到回执" in r.text
                cmds = [m["list"][0]["type"] for m in fake.sent if m.get("list")]
                assert 503 in cmds, f"应当下发 503 获取我的信息: {cmds}"

                # ③ 设备回执到达（缓存有名字）→ 提示已获取，且列表能显示
                aivbot_service.received_data[RB] = {"my_info": [{"info": {"name": "张三"}}]}
                r = await cli.post(f"/admin/robot/{RB}/self_name", follow_redirects=True)
                assert "已获取企微名：张三" in r.text
        finally:
            aivbot_service.active_connections.pop(RB, None)
            aivbot_service.received_data.pop(RB, None)
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM robots WHERE robot_id=%s", (RB,))
                await conn.commit()
            pool.close()
            await pool.wait_closed()

    asyncio.run(main())


@pytestmark_db
def test_online_status_uses_connection_registry():
    """在线状态以 WebSocket 连接注册表为准，而不是 robots.status 那列。

    真机症状（2026-10-09）：设备已连接（active_connections=1），但 DB status 漂成 offline，
    于是「功能测试」页显示"已连接 1 / 暂无在线设备"，并且**设备返回数据整段不渲染**
    （旧代码 `if r.status != "online": continue`），排查时拿不到任何设备回执。
    """
    import httpx

    from app.main import app
    from app.services import (
        agent_service,
        aivbot_service,
        group_service,
        memory_service,
        robot_service,
    )

    class _FakeWS:
        async def send_json(self, msg):
            return None

    async def main():
        pool = await _pool()
        for svc in (robot_service, memory_service, group_service, agent_service):
            svc.set_pool(pool)
        await robot_service.ensure_table()
        try:
            # 设备行：DB 明确写 offline（模拟漂移后的状态）
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM robots WHERE robot_id=%s", (RB,))
                    await cur.execute(
                        "INSERT INTO robots (robot_id,token,device_name,status) "
                        "VALUES (%s,'t','zz在线判定设备','offline')",
                        (RB,),
                    )
                await conn.commit()
            # 但连接注册表里有它（真实在线的样子），并且设备最近有回执数据
            aivbot_service.active_connections[RB] = [_FakeWS()]
            aivbot_service.received_data[RB] = {
                "recent_list": [{"time": "17:00:00", "type": "最近聊天", "count": 1,
                                 "items": ["zz标记数据"], "raw": {}}]
            }

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as cli:
                html = (await cli.get("/test")).text
                assert "暂无在线设备" not in html, "注册表里有连接，不该显示『暂无在线设备』"
                assert RB in html, "设备应出现在在线设备表里"
                # 关键：设备返回数据必须渲染（旧代码因 DB 说离线而整段跳过）
                assert "zz标记数据" in html, "设备返回数据被跳过了（仍在用 DB status 判定）"

                admin = (await cli.get("/admin")).text
                assert RB in admin
                # 统计卡片的在线数也应按注册表算：至少包含这台
                assert "在线" in admin

                st = (await cli.get("/api/status")).json()["data"]
                assert RB in st.get("clients", []), "clients 应来自连接注册表"
                assert st["robots"]["online"] >= 1, "robots.online 应含注册表里的连接"
        finally:
            aivbot_service.active_connections.pop(RB, None)
            aivbot_service.received_data.pop(RB, None)
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM robots WHERE robot_id=%s", (RB,))
                await conn.commit()
            pool.close()
            await pool.wait_closed()

    asyncio.run(main())
