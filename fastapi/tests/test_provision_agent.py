"""「AI 配置组织」——组织配置智能体（agents.kind='provisioner'）的测试。

覆盖：
- 工具契约（名字/参数结构）与提示词不漂移（提示词里必须点名每个工具）
- 配置智能体的工具集**只有** provision_*（早退，不查库）
- 隔离性：不出现在 list_agents（下拉框），include_provisioner=True 才可见
- 门禁：只有配置智能体能执行 provision_*，其它智能体一律被拒
- 真建资源（组织 + 表格 + 授权）落库正确，跑完自清（只碰 zz配置* 自己的行）
- 管理页面能渲染（/admin/orgs 卡片入口、配置页、配置台）

运行：../venv/bin/python -m pytest tests/test_provision_agent.py -q
"""
import asyncio
import importlib
import os
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiomysql  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import provision_service  # noqa: E402
from app.services.provision_service import (  # noqa: E402
    DEFAULT_PROMPT,
    PROVISIONER_DESC,
    PROVISIONER_NAME,
    TOOL_DEFINITIONS,
)

agent_svc_mod = importlib.import_module("app.services.agent_service")
agent_service = agent_svc_mod.agent_service

TOOL_NAMES = sorted(t["function"]["name"] for t in TOOL_DEFINITIONS)
PREFIX = "zz配置测试"


# ── 1. 纯逻辑（不需要 MySQL） ─────────────────────────────


def test_tool_definitions_are_openai_shaped():
    assert TOOL_NAMES, "至少要有工具"
    for t in TOOL_DEFINITIONS:
        assert t["type"] == "function"
        fn = t["function"]
        assert fn["name"].startswith("provision_")
        params = fn["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict) and params["properties"]
        for req in params["required"]:
            assert req in params["properties"], f"{fn['name']} 的必填 {req} 不在 properties 里"
        assert fn["description"].strip()


def test_prompt_mentions_every_tool():
    """提示词与工具会漂移：加了工具忘了写进提示词，模型就永远想不起来用它"""
    for name in TOOL_NAMES:
        assert name in DEFAULT_PROMPT, f"提示词里没有提到工具 {name}"


def test_provisioner_toolset_is_only_provision_tools():
    """配置智能体的工具集在 _build_tools 里早退：不查库、不注入业务工具"""
    pv = SimpleNamespace(id=1, enable_tools=True, kind=agent_svc_mod.PROVISIONER_KIND)
    tools = asyncio.run(agent_service._build_tools(pv, robot_id=""))
    assert sorted(t["function"]["name"] for t in tools) == TOOL_NAMES


def test_normal_agent_kind_default():
    """没写 kind 的 Agent 对象按普通智能体处理（老数据兼容）"""
    assert getattr(SimpleNamespace(id=1), "kind", "chat") != agent_svc_mod.PROVISIONER_KIND


# ── 2. 需要本地 MySQL ─────────────────────────────────────

DB = dict(
    host=settings.MYSQL_HOST,
    port=settings.MYSQL_PORT,
    user=settings.MYSQL_USER,
    password=settings.MYSQL_PASSWORD,
    db=settings.MYSQL_DATABASE,
    charset="utf8mb4",
)

_SINGLETONS = (
    "agent_service",
    "group_service",
    "table_service",
    "workflow_service",
    "knowledge_service",
    "task_service",
    "robot_service",
    "ext_user_service",
    "memory_service",
)


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


requires_db = pytest.mark.skipif(not _mysql_available(), reason="需要本地 MySQL")


def _wire(pool):
    """把池接到各 service 单例上（与 main.py 启动时同一批对象）"""
    for name in _SINGLETONS:
        svc = getattr(importlib.import_module("app.services"), name)
        svc.set_pool(pool)


async def _pool():
    pool = await aiomysql.create_pool(**DB, autocommit=True)
    _wire(pool)
    # agents.kind 是后加的列（ensure_table 里逐条 ALTER），测试自己把表补齐
    await agent_service.ensure_table()
    return pool


async def _id_of(text: str) -> int:
    m = re.search(r"id=(\d+)", text)
    assert m, f"工具返回里没有 id：{text}"
    return int(m.group(1))


async def _del_agent(cur, agent_id: int):
    for sql in (
        "DELETE FROM agent_table_grants WHERE agent_id=%s",
        "DELETE FROM agent_workflows WHERE agent_id=%s",
        "DELETE FROM agent_commands WHERE agent_id=%s",
        "DELETE FROM agent_knowledge WHERE agent_id=%s",
        "DELETE FROM agent_conversations WHERE robot_id IN (SELECT robot_id FROM robots WHERE agent_id=%s)",
        "DELETE FROM robots WHERE agent_id=%s",
        "DELETE FROM agents WHERE id=%s",
    ):
        await cur.execute(sql, (agent_id,))


async def _purge(pool, org_id: int, extra_agents=()):
    """清掉测试建的组织及其成员资源（只按这个 org_id / 这些 agent id）"""
    if not org_id:
        return
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT member_type, member_id FROM org_group_members WHERE group_id=%s",
                (org_id,),
            )
            members = list(await cur.fetchall())
            for mtype, mid in members:
                if mtype == "table":
                    await cur.execute(
                        "SELECT table_key FROM custom_tables WHERE id=%s", (mid,)
                    )
                    row = await cur.fetchone()
                    await cur.execute("DELETE FROM custom_table_fields WHERE table_id=%s", (mid,))
                    await cur.execute("DELETE FROM agent_table_grants WHERE table_id=%s", (mid,))
                    await cur.execute("DELETE FROM custom_tables WHERE id=%s", (mid,))
                    key = (row and row[0]) or f"tbl_{mid}"
                    await cur.execute(f"DROP TABLE IF EXISTS t_{key}")
                elif mtype == "workflow":
                    await cur.execute("DELETE FROM workflow_nodes WHERE workflow_id=%s", (mid,))
                    await cur.execute("DELETE FROM agent_workflows WHERE workflow_id=%s", (mid,))
                    await cur.execute("DELETE FROM command_workflows WHERE id=%s", (mid,))
                elif mtype == "knowledge":
                    await cur.execute("DELETE FROM knowledge_chunks WHERE kb_id=%s", (mid,))
                    await cur.execute("DELETE FROM knowledge_documents WHERE kb_id=%s", (mid,))
                    await cur.execute("DELETE FROM agent_knowledge WHERE kb_id=%s", (mid,))
                    await cur.execute("DELETE FROM knowledge_bases WHERE id=%s", (mid,))
                elif mtype == "agent":
                    await _del_agent(cur, mid)
                elif mtype == "device":
                    await cur.execute("DELETE FROM robots WHERE robot_id=%s", (mid,))
            for aid in extra_agents:
                await _del_agent(cur, aid)
            await cur.execute("DELETE FROM org_group_members WHERE group_id=%s", (org_id,))
            await cur.execute("DELETE FROM org_groups WHERE id=%s", (org_id,))
            await conn.commit()


async def _ensure_pv():
    return await agent_service.ensure_provisioner(
        PROVISIONER_NAME, PROVISIONER_DESC, DEFAULT_PROMPT
    )


@requires_db
def test_provisioner_is_invisible_but_usable():
    async def main():
        pool = await _pool()
        try:
            pv = await _ensure_pv()
            assert pv.kind == agent_svc_mod.PROVISIONER_KIND
            assert pv.name == PROVISIONER_NAME
            assert "组织配置智能体" in pv.system_prompt

            visible = [a.id for a in await agent_service.list_agents()]
            assert pv.id not in visible, "配置智能体不该出现在智能体列表/下拉里"
            all_ids = [a.id for a in await agent_service.list_agents(include_provisioner=True)]
            assert pv.id in all_ids

            names = sorted(
                t["function"]["name"] for t in await agent_service._build_tools(pv, robot_id="")
            )
            assert names == TOOL_NAMES

            assert "未知的配置工具" in await provision_service.execute("provision_nope", {}, pv.id)

            other = next((a for a in await agent_service.list_agents()), None)
            if other:
                txt = await provision_service.execute(
                    "provision_org", {"name": f"{PREFIX}越权"}, other.id
                )
                assert "只有「组织配置智能体」" in txt
        finally:
            pool.close()
            await pool.wait_closed()

    asyncio.run(main())


@requires_db
def test_provision_org_table_grant_and_cleanup():
    async def main():
        from app.models.agent import AgentCreateRequest

        pool = await _pool()
        org_id = 0
        tmp_agent = 0
        try:
            pv = await _ensure_pv()

            org_id = await _id_of(
                await provision_service.execute(
                    "provision_org", {"name": f"{PREFIX}组织", "description": "测试"}, pv.id
                )
            )
            # 同名复用（幂等）
            again = await provision_service.execute(
                "provision_org", {"name": f"{PREFIX}组织"}, pv.id
            )
            assert "已存在" in again and await _id_of(again) == org_id

            table_id = await _id_of(
                await provision_service.execute(
                    "provision_table",
                    {
                        "org_id": org_id,
                        "name": f"{PREFIX}线索表",
                        "fields": [
                            {"name": "customer", "type": "varchar", "label": "客户", "required": True},
                            {"name": "demand", "type": "text", "label": "需求"},
                        ],
                    },
                    pv.id,
                )
            )
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT member_id FROM org_group_members WHERE group_id=%s AND member_type='table'",
                        (org_id,),
                    )
                    assert table_id in [
                        int(r[0]) for r in await cur.fetchall()
                    ], "表格没归到组织"
                    await cur.execute(
                        """SELECT COUNT(*) FROM information_schema.TABLES
                           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s""",
                        (f"t_tbl_{table_id}",),
                    )
                    assert (await cur.fetchone())[0] == 1, "物理表没建出来"

            # 建智能体需要 API Key：配置智能体没配 Key 时必须给出可执行的错误提示
            if not pv.api_key:
                err = await provision_service.execute(
                    "provision_agent", {"org_id": org_id, "name": f"{PREFIX}智能体"}, pv.id
                )
                assert "API Key" in err

            # 授权追加（用测试自建智能体，别碰真实智能体）
            tmp_agent = await agent_service.create_agent(
                AgentCreateRequest(
                    name=f"{PREFIX}智能体",
                    provider="custom",
                    base_url="https://api.deepseek.com",
                    model="deepseek-v4-flash",
                    api_key="sk-zz-provision-test",
                    system_prompt="测试用",
                )
            )
            txt = await provision_service.execute(
                "provision_grant",
                {"agent_id": tmp_agent, "table_ids": [table_id], "command_types": [203]},
                pv.id,
            )
            assert "追加授权" in txt
            grants = await importlib.import_module("app.services").table_service.get_agent_grants(table_id)
            assert int(grants.get(tmp_agent, grants.get(str(tmp_agent), -1))) == 1

            # 汇总能读到刚才建的东西
            summary = await provision_service.execute("provision_summary", {"org_id": org_id}, pv.id)
            assert f"{PREFIX}线索表" in summary
        finally:
            await _purge(pool, org_id, extra_agents=(tmp_agent,))
            pool.close()
            await pool.wait_closed()

    asyncio.run(main())


@requires_db
def test_admin_pages_render():
    async def main():
        import httpx

        from app.main import app

        pool = await _pool()
        try:
            await _ensure_pv()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://lan.test") as c:
                r = await c.get("/admin/orgs")
                assert r.status_code == 200
                assert "/admin/orgs/provisioner" in r.text, "组织选择页没有 AI 配置组织入口"

                r = await c.get("/admin/orgs/provisioner")
                assert r.status_code == 200
                assert "AI 配置组织" in r.text and PROVISIONER_NAME in r.text

                r = await c.get("/admin/orgs/provisioner/chat")
                assert r.status_code == 200 and "配置台" in r.text
        finally:
            pool.close()
            await pool.wait_closed()

    asyncio.run(main())


@requires_db
def test_save_keeps_api_key_when_blank():
    """配置页保存：留空 api_key = 保留原 Key（不许被空值刷掉），跑完还原原配置"""
    async def main():
        import httpx

        from app.main import app

        pool = await _pool()

        async def snapshot():
            async with pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        """SELECT provider,base_url,model,api_key,system_prompt,temperature,
                                  max_tokens,enabled FROM agents WHERE kind=%s ORDER BY id ASC LIMIT 1""",
                        (agent_svc_mod.PROVISIONER_KIND,),
                    )
                    return await cur.fetchone()

        try:
            pv = await _ensure_pv()
            before = await snapshot()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://lan.test") as c:
                r = await c.post(
                    "/admin/orgs/provisioner/save",
                    data={
                        "provider": "deepseek",
                        "base_url": "https://api.deepseek.com",
                        "model": "zz-test-model",
                        "api_key": "",
                        "temperature": before["temperature"],
                        "max_tokens": before["max_tokens"],
                        "system_prompt": before["system_prompt"],
                        "enabled": "1",
                    },
                )
                assert r.status_code == 303
                after = await snapshot()
                assert after["model"] == "zz-test-model"
                assert (after["api_key"] or "") == (before["api_key"] or ""), "空 Key 把原 Key 刷掉了"

            # 还原原配置（直接回写；原本可能是空 base_url，走路由会被校验拦下）
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """UPDATE agents SET provider=%s,base_url=%s,model=%s,system_prompt=%s,
                                  temperature=%s,max_tokens=%s,enabled=%s WHERE id=%s""",
                        (
                            before["provider"], before["base_url"], before["model"],
                            before["system_prompt"], before["temperature"],
                            before["max_tokens"], before["enabled"], pv.id,
                        ),
                    )
                    await conn.commit()
            restored = await snapshot()
            assert restored["model"] == before["model"]
            assert restored["system_prompt"] == before["system_prompt"]
            assert await agent_service.get_provisioner() is not None
            assert pv.id == (await agent_service.get_provisioner()).id
        finally:
            pool.close()
            await pool.wait_closed()

    asyncio.run(main())
