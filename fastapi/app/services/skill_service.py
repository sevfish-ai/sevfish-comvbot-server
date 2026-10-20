"""
技能（Skill）机制：场景化行为指导（软），不承载安全强制。

定位：解决"场景化行为规则写不进 system_prompt"的问题（如"收到某群的某类消息时
执行某特殊工作流且不生成回复"）。按会话上下文（群类型 / 消息特征）匹配技能，
命中后把技能指令注入本次 LLM 调用的 system 消息。

注意：skill 只是给 LLM 的"建议"，安全强制必须由工具授权/工作流校验等硬边界承担。
"""

import json
import logging
import re
from typing import List, Optional

import aiomysql

logger = logging.getLogger(__name__)


def bad_patterns(patterns: List[str]) -> List[str]:
    """返回无法编译的正则（供页面标红/创建时提示），合法则返回空列表"""
    bad = []
    for p in patterns or []:
        try:
            re.compile(str(p), re.MULTILINE)
        except re.error:
            bad.append(str(p))
    return bad


class SkillService:
    def __init__(self):
        self._pool: Optional[aiomysql.Pool] = None

    def set_pool(self, pool: aiomysql.Pool):
        self._pool = pool

    async def ensure_table(self):
        """skills 表：name/description/trigger(JSON)/content/agent_id(可选绑定)/enabled。
        注意：`trigger` 是 MySQL 保留字，列名一律加反引号。"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS skills (
                        id          INT PRIMARY KEY AUTO_INCREMENT,
                        name        VARCHAR(100) NOT NULL,
                        description VARCHAR(500) NOT NULL DEFAULT '',
                        `trigger`   JSON,
                        content     TEXT NOT NULL,
                        agent_id    INT DEFAULT NULL,
                        enabled     TINYINT NOT NULL DEFAULT 1,
                        created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                # 智能体-技能 关联（智能体侧白名单：勾选后才参与触发；旧 skills.agent_id 列废弃不再使用）
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_skills (
                        agent_id INT NOT NULL,
                        skill_id INT NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (agent_id, skill_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                await conn.commit()
        logger.info("✅ skills / agent_skills tables ensured")

    # ── CRUD ────────────────────────────────────────────

    async def list_skills(self) -> List[dict]:
        """全部技能（启用的在前）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, name, description, `trigger`, content, enabled, created_at "
                    "FROM skills ORDER BY enabled DESC, id DESC"
                )
                rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "name": r[1],
                "description": r[2] or "",
                "trigger": json.loads(r[3]) if r[3] else {},
                "content": r[4] or "",
                "enabled": bool(r[5]),
                "created_at": r[6].isoformat() if r[6] else "",
            }
            for r in rows
        ]

    async def create_skill(
        self,
        name: str,
        description: str,
        trigger: dict,
        content: str,
    ) -> int:
        name = (name or "").strip()
        content = (content or "").strip()
        if not name or not content:
            raise ValueError("技能名称与指令内容不能为空")
        if not isinstance(trigger, dict):
            raise ValueError("触发条件必须为 JSON 对象")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO skills (name, description, `trigger`, content, enabled)
                       VALUES (%s,%s,%s,%s,1)""",
                    (
                        name,
                        (description or "").strip(),
                        json.dumps(trigger, ensure_ascii=False),
                        content,
                    ),
                )
                new_id = cur.lastrowid
                await conn.commit()
        logger.info(f"✅ Created skill #{new_id}: {name}")
        return new_id

    async def update_skill(
        self, skill_id: int, name: str, description: str, trigger: dict, content: str
    ) -> bool:
        """编辑技能（不动 enabled：启停由列表页 toggle 管，编辑不该顺手改状态）"""
        if not name or not content:
            raise ValueError("技能名称与指令内容不能为空")
        if not isinstance(trigger, dict):
            raise ValueError("触发条件必须为 JSON 对象")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """UPDATE skills SET name=%s, description=%s, `trigger`=%s, content=%s
                       WHERE id=%s""",
                    (
                        name,
                        (description or "").strip(),
                        json.dumps(trigger, ensure_ascii=False),
                        content,
                        skill_id,
                    ),
                )
                changed = cur.rowcount
                await conn.commit()
        logger.info(f"✏️ Updated skill #{skill_id}: {name} (rows={changed})")
        return bool(changed)

    async def get_skills_by_ids(self, skill_ids: List[int]) -> List[dict]:
        """按 id 查询启用的技能（供工作流 LLM 节点挂载注入）。

        节点挂载 = 管理侧在节点上显式选择，**临时生效、不受智能体勾选约束**：
        只要技能启用且 id 匹配即注入（精确到节点的意图优先级最高）。
        """
        ids = [int(x) for x in skill_ids if str(x).isdigit()]
        if not ids:
            return []
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                fmt = ",".join(["%s"] * len(ids))
                await cur.execute(
                    f"SELECT id, name, description, `trigger`, content "
                    f"FROM skills WHERE enabled=1 AND id IN ({fmt})",
                    tuple(ids),
                )
                rows = await cur.fetchall()
        return [
            {"id": r[0], "name": r[1], "description": r[2] or "", "content": r[4]}
            for r in rows
        ]

    # ── 智能体-技能 绑定（智能体侧白名单） ────────────────

    async def get_agent_skills(self, agent_id: int) -> List[int]:
        """该智能体勾选启用的技能 id 列表"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT skill_id FROM agent_skills WHERE agent_id=%s",
                    (agent_id,),
                )
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def set_agent_skills(self, agent_id: int, skill_ids: List[int]) -> None:
        """全量重设智能体勾选的技能（对齐 set_agent_workflows 模式）"""
        ids = [int(x) for x in (skill_ids or []) if str(x).isdigit()]
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM agent_skills WHERE agent_id=%s", (agent_id,)
                )
                for sid in ids:
                    await cur.execute(
                        "INSERT IGNORE INTO agent_skills (agent_id, skill_id) VALUES (%s,%s)",
                        (agent_id, sid),
                    )
                await conn.commit()

    async def delete_skill(self, skill_id: int) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM skills WHERE id=%s", (skill_id,))
                await cur.execute(
                    "DELETE FROM agent_skills WHERE skill_id=%s", (skill_id,)
                )
                await conn.commit()
        return True

    async def set_enabled(self, skill_id: int, enabled: bool) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE skills SET enabled=%s WHERE id=%s",
                    (1 if enabled else 0, skill_id),
                )
                await conn.commit()
        return True

    # ── 触发匹配 ────────────────────────────────────────

    @staticmethod
    def _compile_ok(pat: str) -> bool:
        try:
            re.compile(pat)
            return True
        except re.error:
            return False

    @staticmethod
    def _split_patterns(raw: str) -> List[str]:
        """正则分隔：一行一条（textarea 输入）。

        不再按逗号拆：`\\d{1,3}(,\\d{3})*元` 这类正则本身含逗号，按逗号切会切成
        三条垃圾（前半段 `\\d{1,3}` 还会命中大量无关文本——静默误触发比报错更难查）。
        兜底：单行且整条编译不过时（如误写 `报修.*,维修.*`）才按逗号拆，兼容老写法。
        """
        raw = raw or ""
        parts = raw.splitlines()
        if len(parts) == 1 and not SkillService._compile_ok(parts[0].strip()):
            parts = raw.split(",")
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def _match_trigger(
        trg: dict, room_type: int, room_name: str, speaker: str, user_text: str
    ) -> bool:
        """trigger 结构（JSON）：
        {
          "room_types": [1, 3],          # 可选：群类型限制（1=外部群 3=内部群，其余为私聊等）
          "keywords": ["报修", "工单"],   # 可选：消息正文包含任一关键词（内容组）
          "patterns": ["报修.*"],        # 可选：消息正文正则命中（内容组）
          "room_keywords": ["供应商"],     # 可选：会话名/群名/私聊对方昵称包含关键词（范围组）
          "speaker_keywords": ["张三"],   # 可选：发送人名称包含关键词（范围组，群聊有效）
        }
        分组语义（避免"范围命中即误触发无关话题"）：
          触发 = 群类型（若配）AND 范围组（若配：会话名 或 成员名 任一命中）AND 内容组（若配：正文关键词 或 正则 任一命中）
          全部未配置 → 永不触发
        """
        if not isinstance(trg, dict):
            return False
        rt = trg.get("room_types")
        keywords = trg.get("keywords") or []
        patterns = trg.get("patterns") or []
        room_kws = trg.get("room_keywords") or []
        speaker_kws = trg.get("speaker_keywords") or []
        # 完全没有任何条件 → 永不触发
        if not (rt or keywords or patterns or room_kws or speaker_kws):
            return False
        # 群类型（若配置）
        if rt:
            try:
                rt_list = [int(x) for x in rt]
            except (TypeError, ValueError):
                rt_list = []
            if rt_list and int(room_type) not in rt_list:
                return False
        # 范围组：会话名 或 成员名（任一命中；都未配置=放行）
        if room_kws or speaker_kws:
            scope_hit = any(k and str(k) in (room_name or "") for k in room_kws)
            scope_hit = scope_hit or any(
                k and str(k) in (speaker or "") for k in speaker_kws
            )
            if not scope_hit:
                return False
        # 内容组：正文关键词 或 正则（任一命中；都未配置=放行）
        if keywords or patterns:
            content_hit = any(k and str(k) in user_text for k in keywords)
            if not content_hit:
                for p in patterns:
                    try:
                        # re.M：多行消息按行锚定（^ 匹配每一行行首），否则 ^ 只认整条消息开头
                        if re.search(str(p), user_text, re.MULTILINE):
                            content_hit = True
                            break
                    except re.error as e:
                        # 非法正则不炸整条链路，但要留痕：否则技能静默失效没法排查
                        logger.warning(f"技能正则非法已跳过 pattern={p!r}: {e}")
                        continue
            if not content_hit:
                return False
        return True

    async def match_skills(
        self,
        agent_id: int,
        room_type: int,
        room_name: str,
        speaker: str,
        user_text: str,
    ) -> List[dict]:
        """按会话上下文匹配该智能体**勾选启用**的技能（智能体侧白名单），返回命中列表。

        语义：技能启用 且 该智能体在 agent_skills 中勾选了它 → 才参与触发匹配。
        节点挂载（get_skills_by_ids）不受此约束，见该方法的注释。
        """
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT s.id, s.name, s.description, s.`trigger`, s.content "
                    "FROM skills s JOIN agent_skills a ON a.skill_id = s.id "
                    "WHERE s.enabled=1 AND a.agent_id=%s",
                    (agent_id,),
                )
                rows = await cur.fetchall()
        matched = []
        for r in rows:
            trg = json.loads(r[3]) if r[3] else {}
            if self._match_trigger(trg, room_type, room_name, speaker, user_text):
                matched.append({"id": r[0], "name": r[1], "content": r[4]})
        return matched


# 模块单例（与其他 service 一致：从模块导入同一实例）
skill_service = SkillService()
