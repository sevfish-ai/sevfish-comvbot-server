"""pytest 全局夹具。

这个项目的测试**直接连本地真实的 `aivbot` 库**（不是独立的测试库），所以任何
"DROP TABLE / 无 WHERE 的 DELETE" 都会动到正在运行的服务端数据 —— 已经真的发生过：
长期记忆的迁移用例里写了 `DROP TABLE IF EXISTS robot_memory_sets`，把测试机上配好的
设备记忆绑定整表清空，导致真机联调时记忆莫名其妙不生效（排查了很久）。

这里给"用户配置类"的表加一层保险：跑测试前快照，跑完还原。
只保护**配置**，不保护测试自己造的业务数据（memory_items/customers 各用例自建自清）。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiomysql  # noqa: E402

from app.config import settings  # noqa: E402

# 需要保护的"用户配置"：设备↔记忆集绑定、记忆集的组织归属
_GUARDED = (
    ("robot_memory_sets", "SELECT robot_id,set_id,mode FROM robot_memory_sets"),
    (
        "org_group_members",
        "SELECT group_id,member_type,member_id FROM org_group_members WHERE member_type='memory'",
    ),
)


async def _snapshot():
    pool = await aiomysql.create_pool(
        host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD, db=settings.MYSQL_DATABASE,
        charset="utf8mb4", autocommit=True,
    )
    snap = {}
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for table, sql in _GUARDED:
                    try:
                        await cur.execute(sql)
                        snap[table] = await cur.fetchall()
                    except Exception:
                        snap[table] = None  # 表还不存在（全新环境）
    finally:
        pool.close()
        await pool.wait_closed()
    return snap


async def _restore(snap):
    pool = await aiomysql.create_pool(
        host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD, db=settings.MYSQL_DATABASE,
        charset="utf8mb4", autocommit=True,
    )
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                rows = snap.get("robot_memory_sets")
                if rows is not None:
                    await cur.execute("DELETE FROM robot_memory_sets")
                    for r in rows:
                        await cur.execute(
                            "INSERT IGNORE INTO robot_memory_sets (robot_id,set_id,mode) VALUES (%s,%s,%s)", r
                        )
                rows = snap.get("org_group_members")
                if rows is not None:
                    await cur.execute(
                        "DELETE FROM org_group_members WHERE member_type='memory'"
                    )
                    for r in rows:
                        await cur.execute(
                            "INSERT IGNORE INTO org_group_members (group_id,member_type,member_id) "
                            "VALUES (%s,%s,%s)",
                            r,
                        )
                await conn.commit()
    finally:
        pool.close()
        await pool.wait_closed()


@pytest.fixture(scope="session", autouse=True)
def _guard_live_config():
    """整轮测试前后快照/还原"用户配置"表（连的是真库，必须防手滑）。"""
    try:
        snap = asyncio.run(_snapshot())
    except Exception:
        yield  # 连不上库（纯离线跑）→ 不做保护，也不阻塞
        return
    try:
        yield
    finally:
        try:
            asyncio.run(_restore(snap))
        except Exception as e:  # 还原失败要吵出来，别静默
            print(f"⚠️ 用户配置还原失败: {e}")
