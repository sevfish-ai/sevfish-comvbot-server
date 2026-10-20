"""技能触发匹配：正则能力（多行锚点 / 含逗号正则 / 一行一条 / 非法正则不炸）。

回归背景：patterns 字段一直走 re.search，但存在三个把正则搞坏的坑
  1. 表单是单行 input，多行正则被折成一行 → ^ 锚点失效
  2. 按逗号拆正则 → 含逗号的正则被切碎，前半段静默误触发
  3. 非法正则静默 continue → 技能失效无日志
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.skill_service import SkillService, bad_patterns  # noqa: E402

match = SkillService._match_trigger
split = SkillService._split_patterns


def test_split_one_per_line():
    assert split(r"工单号[:：]\s*\d{10}" + "\n" + "^(报修|维修)[:：]") == [
        r"工单号[:：]\s*\d{10}",
        "^(报修|维修)[:：]",
    ]


def test_split_keeps_regex_with_comma_whole():
    """含逗号的正则必须整条保留，不能被切成 `\\d{1` / `3}(` 这种垃圾"""
    assert split(r"\d{1,3}(,\d{3})*元") == [r"\d{1,3}(,\d{3})*元"]


def test_split_strips_blank_lines():
    assert split("a\n\n  \nb") == ["a", "b"]


def test_regex_with_comma_matches_and_does_not_misfire():
    pats = split(r"\d{1,3}(,\d{3})*元")
    assert match({"patterns": pats}, 3, "群", "", "金额 1,234元") is True
    assert match({"patterns": pats}, 3, "群", "", "聊了 5 句") is False


def test_multiline_anchor():
    """re.M：^ 应匹配多行正文的每一行行首"""
    assert match({"patterns": ["^财务"]}, 3, "群", "", "a\n财务部通知") is True


def test_invalid_regex_skipped_without_crash():
    assert match({"patterns": ["[unclosed"]}, 3, "群", "", "任意文本") is False
    assert bad_patterns(["[unclosed", r"\d+"]) == ["[unclosed"]


def test_group_semantics_still_hold():
    """范围组与内容组必须都命中（防止技能改坏分组语义）"""
    trg = {"room_keywords": ["供应商"], "patterns": [r"^报修"]}
    assert match(trg, 1, "供应商群", "", "报修空调") is True
    assert match(trg, 1, "客户群", "", "报修空调") is False
    assert match(trg, 1, "供应商群", "", "今天天气不错") is False


def test_empty_trigger_never_fires():
    assert match({}, 1, "群", "", "任何消息") is False
    assert match({"room_types": [], "patterns": []}, 1, "群", "", "任何消息") is False


# ── 编辑功能：共享表单 + trigger 解析 ──────────────────────

from app.routes.skills import _build_trigger, _skill_form  # noqa: E402


def test_form_backfills_trigger_and_content():
    """编辑表单必须回填 trigger 各字段与指令正文，否则一保存就把技能清空"""
    sk = {
        "id": 7,
        "name": "报修登记",
        "description": "登记工单",
        "trigger": {
            "room_types": [1, 3],
            "keywords": ["报修", "维修"],
            "patterns": [r"\d{1,3}(,\d{3})*元", "^(报修|维修)[:：]"],
            "room_keywords": ["供应商"],
            "speaker_keywords": ["张三"],
        },
        "content": "调用 run_workflow_5 登记",
    }
    html = _skill_form("/admin/skills/7/update", 0, sk)
    assert 'action="/admin/skills/7/update"' in html
    assert 'value="报修登记"' in html
    assert 'value="报修,维修"' in html
    assert 'value="供应商"' in html
    assert 'value="张三"' in html
    assert 'value="1" checked' in html and 'value="3" checked' in html
    assert 'value="2" checked' not in html
    # 多行正则回填成多行 textarea 内容（含逗号的那条必须完整）
    assert r"\d{1,3}(,\d{3})*元" in html
    assert "^(报修|维修)[:：]" in html
    assert "调用 run_workflow_5 登记" in html
    assert "保存修改" in html


def test_form_create_mode_is_blank_and_posts_to_create():
    html = _skill_form("/admin/skills/create")
    assert 'action="/admin/skills/create"' in html
    assert "创建技能" in html
    assert "保存修改" not in html


def test_form_escapes_quotes_in_values():
    """名称含双引号时不能截断 value 属性"""
    html = _skill_form("/admin/skills/1/update", 0, {"id": 1, "name": 'a"b', "content": "c"})
    assert 'value="a&quot;b"' in html


def test_build_trigger_rejects_invalid_regex():
    import pytest

    with pytest.raises(ValueError, match="正则无法编译"):
        _build_trigger([1], "报修", "[unclosed", "", "")


def test_build_trigger_parses_fields():
    trg = _build_trigger([1, 3], "报修, 维修", r"\d+", "供应商", "张三")
    assert trg["room_types"] == [1, 3]
    assert trg["keywords"] == ["报修", "维修"]
    assert trg["patterns"] == [r"\d+"]
    assert trg["room_keywords"] == ["供应商"]
    assert trg["speaker_keywords"] == ["张三"]


# ── 端到端：消息 → generate_reply → 技能注入 LLM 的 system ──────────
# 覆盖真实链路：match_skills 的 DB 查询（skills JOIN agent_skills）+ 注入位置。
# 只 monkeypatch 最外层 _chat_completion，拦截「实际发给 LLM 的 messages」，不 mock 业务逻辑。

import asyncio  # noqa: E402

import aiomysql  # noqa: E402

from app.services.agent_service import agent_service as ags  # noqa: E402
from app.services.skill_service import skill_service as sks  # noqa: E402
from app.config import settings  # noqa: E402

DB = dict(host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
          password=settings.MYSQL_PASSWORD, db=settings.MYSQL_DATABASE, charset="utf8mb4")
TEST_SKILL_ID = 990001
TEST_AGENT_ID = 990001
TEST_ROBOT = "zz-e2e-01"


async def _prepare(pool):
    """建隔离测试技能 + 测试智能体 + 勾选关联（只用 test 前缀 id，不碰真实数据）"""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM agent_skills WHERE agent_id=%s OR skill_id=%s", (TEST_AGENT_ID, TEST_SKILL_ID))
            await cur.execute("DELETE FROM skills WHERE id=%s", (TEST_SKILL_ID,))
            await cur.execute("DELETE FROM agents WHERE id=%s", (TEST_AGENT_ID,))
            trg = {
                "room_types": [3],
                "patterns": [r"工单号[:：]\s*\d{10}", r"\d{1,3}(,\d{3})*元"],
                "keywords": [],
                "room_keywords": [],
                "speaker_keywords": [],
            }
            await cur.execute(
                "INSERT INTO skills (id,name,description,`trigger`,content,enabled) VALUES (%s,%s,%s,%s,%s,1)",
                (TEST_SKILL_ID, "正则触发技能测试", "e2e", json.dumps(trg, ensure_ascii=False), "注入标记：ZZ_SKILL_INJECTED"),
            )
            await cur.execute(
                """INSERT INTO agents (id,name,description,provider,base_url,model,api_key,system_prompt,enabled)
                   VALUES (%s,%s,'','custom','http://127.0.0.1:1','m','k','ZZ_BASE_PROMPT',1)""",
                (TEST_AGENT_ID, "zz技能测试智能体"),
            )
            await cur.execute("INSERT INTO agent_skills (agent_id,skill_id) VALUES (%s,%s)", (TEST_AGENT_ID, TEST_SKILL_ID))
            await conn.commit()


async def _cleanup(pool):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for sql, args in (
                ("DELETE FROM agent_skills WHERE agent_id=%s OR skill_id=%s", (TEST_AGENT_ID, TEST_SKILL_ID)),
                ("DELETE FROM skills WHERE id=%s", (TEST_SKILL_ID,)),
                ("DELETE FROM agents WHERE id=%s", (TEST_AGENT_ID,)),
                ("DELETE FROM agent_conversations WHERE robot_id=%s", (TEST_ROBOT,)),
            ):
                await cur.execute(sql, args)
            await conn.commit()


async def _pool_close(pool):
    """关池 + 断掉连接持有的 loop 引用，避免 loop 关闭后 GC 报 "Event loop is closed" """
    pool.close()
    await pool.wait_closed()
    for conn in list(getattr(pool, "_free", []) or []):
        conn._loop = None


async def _e2e():
    """返回 (命中场景的 system 提示词, 未命中场景的 system 提示词)"""
    pool = await aiomysql.create_pool(**DB, autocommit=True)
    captured = []
    orig_chat = ags._chat_completion

    async def fake_chat(agent, messages, **kw):
        captured.append("\n".join(m.get("content", "") for m in messages if m.get("role") == "system"))
        return "OK"

    try:
        sks.set_pool(pool)
        ags.set_pool(pool)
        await _prepare(pool)
        agent = await ags.get_agent(TEST_AGENT_ID)
        ags._chat_completion = fake_chat

        # 场景1：正则命中（工单号格式）
        await ags.generate_reply(
            agent=agent, robot_id=TEST_ROBOT, room_name="内部测试群", room_type=3,
            user_text="工单号：1234567890 麻烦处理下",
        )
        # 场景2：正则命中（含逗号正则分支）
        await ags.generate_reply(
            agent=agent, robot_id=TEST_ROBOT, room_name="内部测试群", room_type=3,
            user_text="金额 1,234元",
        )
        # 场景3：正则不命中
        await ags.generate_reply(
            agent=agent, robot_id=TEST_ROBOT, room_name="内部测试群", room_type=3,
            user_text="今天天气不错",
        )
        # 场景4：群类型不匹配（配的是内部群3，这里发外部群1）
        await ags.generate_reply(
            agent=agent, robot_id=TEST_ROBOT, room_name="外部测试群", room_type=1,
            user_text="工单号：1234567890",
        )
    finally:
        ags._chat_completion = orig_chat
        try:
            await _cleanup(pool)
        finally:
            await _pool_close(pool)
    assert len(captured) == 4, f"应有 4 次 LLM 调用，实际 {len(captured)}"
    return captured


def test_regex_match_injects_skill_into_llm_system():
    """正则命中 → 技能指令必须真的出现在发给 LLM 的 system 里；前缀保持智能体人设"""
    hit_fmt, hit_comma, miss, wrong_room = asyncio.run(_e2e())

    # 场景1：工单号正则命中
    assert "ZZ_BASE_PROMPT" in hit_fmt, "智能体 system_prompt 必须保留"
    assert "【技能：正则触发技能测试】" in hit_fmt, "技能块未注入 LLM"
    assert "ZZ_SKILL_INJECTED" in hit_fmt, "技能指令正文未注入 LLM"
    assert hit_fmt.index("ZZ_BASE_PROMPT") < hit_fmt.index("ZZ_SKILL_INJECTED"), "技能应追加在人设之后"

    # 场景2：含逗号的正则整条生效，不再被逗号切碎
    assert "ZZ_SKILL_INJECTED" in hit_comma

    # 场景3：正文不匹配 → 不注入
    assert "ZZ_SKILL_INJECTED" not in miss
    assert "ZZ_BASE_PROMPT" in miss, "未命中技能时仍应正常调用 LLM"

    # 场景4：群类型不匹配 → 不注入
    assert "ZZ_SKILL_INJECTED" not in wrong_room


def test_skill_requires_agent_whitelist():
    """技能启用但智能体没勾选 → 不注入（智能体侧白名单这道门必须有）"""
    async def run():
        pool = await aiomysql.create_pool(**DB, autocommit=True)
        captured = []
        orig_chat = ags._chat_completion

        async def fake_chat(agent, messages, **kw):
            captured.append("\n".join(m.get("content", "") for m in messages if m.get("role") == "system"))
            return "OK"

        try:
            sks.set_pool(pool)
            ags.set_pool(pool)
            await _prepare(pool)
            # 取消勾选（只删关联，技能本身仍启用）
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM agent_skills WHERE agent_id=%s", (TEST_AGENT_ID,))
                    await conn.commit()
            agent = await ags.get_agent(TEST_AGENT_ID)
            ags._chat_completion = fake_chat
            await ags.generate_reply(
                agent=agent, robot_id=TEST_ROBOT, room_name="内部测试群", room_type=3,
                user_text="工单号：1234567890",
            )
        finally:
            ags._chat_completion = orig_chat
            try:
                await _cleanup(pool)
            finally:
                await _pool_close(pool)
        return captured[0]

    system = asyncio.run(run())
    assert "ZZ_SKILL_INJECTED" not in system


# ── 静默异常兜底：后台任务异常必须留痕 ──────────────────────
# 回归背景：`add_done_callback(set.discard)` 从不调用 task.exception()，
# 异常无人认领 → 链路静默降级（技能触发就这么坏了 3 天）。

import gc  # noqa: E402

from app.services.aivbot_service import (  # noqa: E402
    install_loop_exception_handler,
    log_task_exception,
)


def test_log_task_exception_logs_error(caplog):
    async def run():
        async def boom():
            raise RuntimeError("后台炸了")
        t = asyncio.get_running_loop().create_task(boom(), name="boom-task")
        try:
            await t
        except RuntimeError:
            pass
        return t

    t = asyncio.run(run())
    with caplog.at_level("ERROR"):
        log_task_exception(t)
    assert any("后台任务异常" in r.message and "boom-task" in r.message for r in caplog.records)
    assert any(r.exc_info for r in caplog.records), "必须带堆栈，否则又回到一行消息查不出来"


def test_log_task_exception_ignores_cancelled_and_success(caplog):
    async def run():
        async def ok():
            return 1

        async def slow():
            await asyncio.sleep(30)
        t_ok = asyncio.get_running_loop().create_task(ok())
        t_c = asyncio.get_running_loop().create_task(slow())
        await t_ok
        t_c.cancel()
        try:
            await t_c
        except asyncio.CancelledError:
            pass
        return t_ok, t_c

    t_ok, t_c = asyncio.run(run())
    with caplog.at_level("ERROR"):
        log_task_exception(t_ok)
        log_task_exception(t_c)
    assert not caplog.records, "成功/取消的任务不该告警"


def test_loop_exception_handler_catches_unretrieved_task_exception(caplog):
    """无人认领的任务异常必须被全局处理器记下（含堆栈）"""
    async def run():
        seen = []
        install_loop_exception_handler()
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, ctx: seen.append(ctx)
        )
        # 重新装一次：确认我们的处理器会调用被替换的 default 之外，自己也留痕
        install_loop_exception_handler()
        async def boom():
            raise ValueError("没人接的异常")
        t = asyncio.get_running_loop().create_task(boom(), name="orphan")
        del t
        await asyncio.sleep(0.05)
        gc.collect()
        await asyncio.sleep(0.05)
        return seen

    with caplog.at_level("ERROR"):
        seen = asyncio.run(run())
    assert any("未处理的异步异常" in r.message for r in caplog.records), caplog.text
    assert any(r.exc_info for r in caplog.records), "全局兜底也必须带堆栈"
    # 处理器不吞掉异常：context 仍会转发给原处理器（这里被测试替身拦截）
    assert any(c.get("exception") for c in seen), "不应吞掉 context"
