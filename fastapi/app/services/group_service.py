"""
Device group service (MySQL backed)

Groups are many-to-many with robots: a device can belong to multiple groups.

同时支持智能体 / 知识库 / 表格 / 工作流分组（同构的分组表 + 成员表），
统一通过 kind 区分：device / agent / knowledge / table / workflow。
"""

import logging
from typing import List, Optional

import aiomysql

from ..models.group import GroupInfo

logger = logging.getLogger(__name__)

# 五类分组的表映射（避免 MySQL 保留字 groups）
_KIND_TABLES = {
    "device": ("device_groups", "robot_groups", "robot_id"),
    "agent": ("agent_groups", "agent_group_members", "agent_id"),
    "knowledge": ("knowledge_groups", "knowledge_group_members", "kb_id"),
    "table": ("custom_table_groups", "custom_table_group_members", "table_id"),
    "workflow": ("custom_workflow_groups", "custom_workflow_group_members", "workflow_id"),
}


class GroupService:
    """Service for device group management using MySQL"""

    def __init__(self):
        self._pool: Optional[aiomysql.Pool] = None

    def set_pool(self, pool: aiomysql.Pool):
        """Set the database connection pool"""
        self._pool = pool

    async def ensure_table(self):
        """Create device_groups / robot_groups tables if not exists

        NOTE: 表名不用 `groups` —— GROUPS 是 MySQL 8.0 保留字（窗口函数）。
        """
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS device_groups (
                        id         INT AUTO_INCREMENT PRIMARY KEY,
                        name       VARCHAR(100) NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_group_name (name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS robot_groups (
                        robot_id  VARCHAR(10) NOT NULL,
                        group_id  INT NOT NULL,
                        PRIMARY KEY (robot_id, group_id),
                        KEY idx_group_id (group_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                # 智能体分组
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_groups (
                        id         INT AUTO_INCREMENT PRIMARY KEY,
                        name       VARCHAR(100) NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_agent_group_name (name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_group_members (
                        agent_id INT NOT NULL,
                        group_id INT NOT NULL,
                        PRIMARY KEY (agent_id, group_id),
                        KEY idx_ag_group (group_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                # 知识库分组
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_groups (
                        id         INT AUTO_INCREMENT PRIMARY KEY,
                        name       VARCHAR(100) NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_kb_group_name (name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_group_members (
                        kb_id    INT NOT NULL,
                        group_id INT NOT NULL,
                        PRIMARY KEY (kb_id, group_id),
                        KEY idx_kg_group (group_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                # 表格分组
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS custom_table_groups (
                        id         INT AUTO_INCREMENT PRIMARY KEY,
                        name       VARCHAR(100) NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_table_group_name (name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS custom_table_group_members (
                        table_id INT NOT NULL,
                        group_id INT NOT NULL,
                        PRIMARY KEY (table_id, group_id),
                        KEY idx_tg_group (group_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                # 工作流分组
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS custom_workflow_groups (
                        id         INT AUTO_INCREMENT PRIMARY KEY,
                        name       VARCHAR(100) NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_wf_group_name (name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_group_members (
                        kb_id    INT NOT NULL,
                        group_id INT NOT NULL,
                        PRIMARY KEY (kb_id, group_id),
                        KEY idx_kg_group (group_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                # ── 统一公司分组（org）：设备/智能体/知识库/表格/工作流共用一套分组 ──
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS org_groups (
                        id          INT AUTO_INCREMENT PRIMARY KEY,
                        name        VARCHAR(100) NOT NULL,
                        description VARCHAR(500) NOT NULL DEFAULT '',
                        created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_org_group_name (name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS org_group_members (
                        group_id    INT         NOT NULL,
                        member_type VARCHAR(20) NOT NULL,
                        member_id   VARCHAR(64) NOT NULL,
                        PRIMARY KEY (group_id, member_type, member_id),
                        KEY idx_member (member_type, member_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await conn.commit()
        logger.info("✅ Groups tables ensured")

    # ── helper ────────────────────────────────────────────

    async def _member_ids(self, group_id: int) -> List[str]:
        """Get member robot IDs of a group"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT robot_id FROM robot_groups WHERE group_id=%s",
                    (group_id,),
                )
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def _group_from_row(self, row, robots: List[str] = None) -> GroupInfo:
        return GroupInfo(
            id=row[0],
            name=row[1],
            created_at=row[2].isoformat() if row[2] else "",
            member_count=len(robots) if robots is not None else 0,
            robots=robots or [],
        )

    # ── core operations ───────────────────────────────────

    async def create_group(self, name: str) -> GroupInfo:
        """Create a new group"""
        name = name.strip()
        if not name:
            raise ValueError("group name must not be empty")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "INSERT INTO device_groups (name) VALUES (%s)", (name,)
                    )
                    await conn.commit()
                    group_id = cur.lastrowid
                except aiomysql.IntegrityError:
                    raise ValueError(f"group name already exists: {name}")
        return await self.get_group(group_id)

    async def list_groups(self) -> List[GroupInfo]:
        """List all groups with member counts and member IDs"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, name, created_at FROM device_groups ORDER BY id ASC"
                )
                rows = await cur.fetchall()
        groups = []
        for row in rows:
            robots = await self._member_ids(row[0])
            groups.append(await self._group_from_row(row, robots))
        return groups

    async def get_group(self, group_id: int) -> Optional[GroupInfo]:
        """Get a group by ID (with members)"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, name, created_at FROM device_groups WHERE id=%s",
                    (group_id,),
                )
                row = await cur.fetchone()
        if not row:
            return None
        robots = await self._member_ids(group_id)
        return await self._group_from_row(row, robots)

    async def delete_group(self, group_id: int) -> bool:
        """Delete a group and its memberships"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM robot_groups WHERE group_id=%s", (group_id,))
                await cur.execute("DELETE FROM device_groups WHERE id=%s", (group_id,))
                await conn.commit()
                affected = cur.rowcount
        if affected:
            logger.info(f"Deleted group: {group_id}")
            return True
        return False

    async def add_robot(self, group_id: int, robot_id: str) -> bool:
        """Add a robot to a group. Returns False if group missing."""
        if not await self.get_group(group_id):
            return False
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "INSERT INTO robot_groups (robot_id, group_id) VALUES (%s, %s)",
                        (robot_id, group_id),
                    )
                    await conn.commit()
                except aiomysql.IntegrityError:
                    pass  # already a member
        logger.info(f"Added robot {robot_id} to group {group_id}")
        return True

    async def remove_robot(self, group_id: int, robot_id: str) -> bool:
        """Remove a robot from a group. Returns False if group missing."""
        if not await self.get_group(group_id):
            return False
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM robot_groups WHERE group_id=%s AND robot_id=%s",
                    (group_id, robot_id),
                )
                await conn.commit()
        logger.info(f"Removed robot {robot_id} from group {group_id}")
        return True

    async def get_robot_groups(self, robot_id: str) -> List[int]:
        """Get group IDs that a robot belongs to"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT group_id FROM robot_groups WHERE robot_id=%s",
                    (robot_id,),
                )
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    # ════════════════════════════════════════════
    #  通用分组（device / agent / knowledge）
    # ════════════════════════════════════════════

    @staticmethod
    def _kind_tables(kind: str):
        tables = _KIND_TABLES.get(kind)
        if not tables:
            raise ValueError(
                f"无效的分组类型: {kind}（支持 device/agent/knowledge/table/workflow）"
            )
        return tables

    async def list_groups_of(self, kind: str) -> List[dict]:
        """按类型列出全部分组（含成员数）。返回 [{"id","name","created_at","member_count"}]"""
        gtable, mtable, mcol = self._kind_tables(kind)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""SELECT g.id, g.name, g.created_at,
                               (SELECT COUNT(*) FROM {mtable} m WHERE m.group_id = g.id)
                        FROM {gtable} g ORDER BY g.id ASC"""
                )
                rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "name": r[1],
                "created_at": r[2].isoformat() if r[2] else "",
                "member_count": r[3] or 0,
            }
            for r in rows
        ]

    async def get_group_of(self, kind: str, group_id: int) -> Optional[dict]:
        """获取分组详情（含成员 id 列表）"""
        gtable, mtable, mcol = self._kind_tables(kind)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT id, name, created_at FROM {gtable} WHERE id=%s", (group_id,)
                )
                row = await cur.fetchone()
                if not row:
                    return None
                await cur.execute(
                    f"SELECT {mcol} FROM {mtable} WHERE group_id=%s", (group_id,)
                )
                mrows = await cur.fetchall()
        members = [m[0] for m in mrows]
        return {
            "id": row[0],
            "name": row[1],
            "created_at": row[2].isoformat() if row[2] else "",
            "member_count": len(members),
            "members": members,
        }

    async def create_group_of(self, kind: str, name: str) -> int:
        """创建分组，返回新分组 id"""
        gtable, _, _ = self._kind_tables(kind)
        name = (name or "").strip()
        if not name:
            raise ValueError("分组名称不能为空")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        f"INSERT INTO {gtable} (name) VALUES (%s)", (name,)
                    )
                    await conn.commit()
                    return cur.lastrowid
                except aiomysql.IntegrityError as e:
                    raise ValueError(f"分组「{name}」已存在") from e

    async def delete_group_of(self, kind: str, group_id: int) -> bool:
        """删除分组及其成员关系"""
        gtable, mtable, _ = self._kind_tables(kind)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM {mtable} WHERE group_id=%s", (group_id,)
                )
                await cur.execute(
                    f"DELETE FROM {gtable} WHERE id=%s", (group_id,)
                )
                await conn.commit()
                affected = cur.rowcount
        return affected > 0

    async def add_member_of(self, kind: str, group_id: int, member_id) -> bool:
        """将成员加入分组。member_id 为 str（设备）或 int（智能体/知识库）"""
        _, mtable, mcol = self._kind_tables(kind)
        if not await self.get_group_of(kind, group_id):
            return False
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        f"INSERT INTO {mtable} ({mcol}, group_id) VALUES (%s, %s)",
                        (member_id, group_id),
                    )
                    await conn.commit()
                except aiomysql.IntegrityError:
                    pass  # already member
        return True

    async def remove_member_of(self, kind: str, group_id: int, member_id) -> bool:
        """将成员移出分组"""
        _, mtable, mcol = self._kind_tables(kind)
        if not await self.get_group_of(kind, group_id):
            return False
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM {mtable} WHERE group_id=%s AND {mcol}=%s",
                    (group_id, member_id),
                )
                await conn.commit()
        return True

    async def get_member_groups(self, kind: str, member_id) -> List[int]:
        """获取某成员所属分组 id 列表"""
        _, mtable, mcol = self._kind_tables(kind)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT group_id FROM {mtable} WHERE {mcol}=%s", (member_id,)
                )
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def set_member_groups(self, kind: str, member_id, group_ids: List[int]) -> None:
        """全量重设某成员的所属分组"""
        _, mtable, mcol = self._kind_tables(kind)
        group_ids = [int(x) for x in (group_ids or []) if str(x).strip()]
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM {mtable} WHERE {mcol}=%s", (member_id,)
                )
                for gid in group_ids:
                    await cur.execute(
                        f"INSERT IGNORE INTO {mtable} ({mcol}, group_id) VALUES (%s,%s)",
                        (member_id, gid),
                    )
                await conn.commit()

    # ════════════════════════════════════════════
    #  统一公司分组（org）—— 设备/智能体/知识库/长期记忆/表格/工作流共用一套分组
    # ════════════════════════════════════════════

    _MEMBER_TYPES = ("device", "agent", "knowledge", "memory", "table", "workflow", "task")

    @staticmethod
    def _check_member_type(member_type: str):
        if member_type not in GroupService._MEMBER_TYPES:
            raise ValueError(
                f"无效的成员类型: {member_type}（支持 {GroupService._MEMBER_TYPES}）"
            )

    async def list_org_groups(self) -> List[dict]:
        """列出全部公司分组（含成员总数及各类成员数量）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT g.id, g.name, g.description, g.created_at, "
                    "(SELECT COUNT(*) FROM org_group_members m WHERE m.group_id = g.id) "
                    "FROM org_groups g ORDER BY g.id ASC"
                )
                rows = await cur.fetchall()
        groups = [
            {
                "id": r[0],
                "name": r[1],
                "description": r[2] or "",
                "created_at": r[3].isoformat() if r[3] else "",
                "member_count": r[4] or 0,
            }
            for r in rows
        ]
        # 各类型成员数量: {group_id: {member_type: count}}
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT group_id, member_type, COUNT(*) "
                    "FROM org_group_members GROUP BY group_id, member_type"
                )
                cnt_rows = await cur.fetchall()
        cnt_map: dict = {}
        for gid, mt, cnt in cnt_rows:
            cnt_map.setdefault(gid, {})[mt] = cnt
        # 任务按 tasks.org_id 列归属（其余资源走 org_group_members 关联表）
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT org_id, COUNT(*) FROM tasks GROUP BY org_id")
                t_rows = await cur.fetchall()
        for gid, cnt in t_rows:
            cnt_map.setdefault(gid, {})["task"] = cnt
        for g in groups:
            g["member_counts"] = cnt_map.get(g["id"], {})
        return groups

    async def create_org_group(self, name: str, description: str = "") -> int:
        """创建公司分组，返回分组 id"""
        name = (name or "").strip()
        if not name:
            raise ValueError("分组名称不能为空")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "INSERT INTO org_groups (name, description) VALUES (%s, %s)",
                        (name, (description or "").strip()),
                    )
                    await conn.commit()
                    return cur.lastrowid
                except aiomysql.IntegrityError as e:
                    raise ValueError(f"分组「{name}」已存在") from e

    async def delete_org_group(self, group_id: int) -> bool:
        """删除公司分组及其全部成员关系"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM org_group_members WHERE group_id=%s", (group_id,)
                )
                await cur.execute(
                    "DELETE FROM org_groups WHERE id=%s", (group_id,)
                )
                await conn.commit()
                return cur.rowcount > 0

    async def get_org_group(self, group_id: int) -> Optional[dict]:
        """获取分组详情，members 按类型分组返回 id 列表"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, name, description, created_at FROM org_groups WHERE id=%s",
                    (group_id,),
                )
                row = await cur.fetchone()
                if not row:
                    return None
                await cur.execute(
                    "SELECT member_type, member_id FROM org_group_members WHERE group_id=%s",
                    (group_id,),
                )
                mrows = await cur.fetchall()
                # 任务按 tasks.org_id 列归属（与 list_org_groups 统计口径一致）
                await cur.execute(
                    "SELECT id FROM tasks WHERE org_id=%s", (group_id,)
                )
                trows = await cur.fetchall()
        members: dict = {t: [] for t in self._MEMBER_TYPES}
        for mt, mid in mrows:
            members.setdefault(mt, []).append(str(mid))
        for r in trows:
            members.setdefault("task", []).append(str(r[0]))
        return {
            "id": row[0],
            "name": row[1],
            "description": row[2] or "",
            "created_at": row[3].isoformat() if row[3] else "",
            "members": members,
        }

    # ── 资源侧：设置资源属于哪些分组 ──

    async def get_member_org_groups(self, member_type: str, member_id) -> List[int]:
        """某资源（成员）所属的公司分组 id 列表"""
        self._check_member_type(member_type)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT group_id FROM org_group_members "
                    "WHERE member_type=%s AND member_id=%s",
                    (member_type, str(member_id)),
                )
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def set_member_org_groups(
        self, member_type: str, member_id, group_ids: List[int]
    ) -> None:
        """全量重设某资源所属的公司分组"""
        self._check_member_type(member_type)
        gids = [int(x) for x in (group_ids or []) if str(x).strip()]
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM org_group_members WHERE member_type=%s AND member_id=%s",
                    (member_type, str(member_id)),
                )
                for gid in gids:
                    await cur.execute(
                        "INSERT IGNORE INTO org_group_members "
                        "(group_id, member_type, member_id) VALUES (%s, %s, %s)",
                        (gid, member_type, str(member_id)),
                    )
                await conn.commit()

    # ── 分组侧：管理分组成员 ──

    async def get_group_org_members(self, group_id: int, member_type: str) -> List[str]:
        """某分组中某类成员 id 列表"""
        self._check_member_type(member_type)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT member_id FROM org_group_members "
                    "WHERE group_id=%s AND member_type=%s",
                    (group_id, member_type),
                )
                rows = await cur.fetchall()
        return [str(r[0]) for r in rows]

    async def add_group_org_member(
        self, group_id: int, member_type: str, member_id
    ) -> bool:
        """向分组添加一个成员"""
        self._check_member_type(member_type)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "INSERT IGNORE INTO org_group_members "
                        "(group_id, member_type, member_id) VALUES (%s, %s, %s)",
                        (group_id, member_type, str(member_id)),
                    )
                    await conn.commit()
                except Exception:
                    pass
        return True

    async def remove_group_org_member(
        self, group_id: int, member_type: str, member_id
    ) -> bool:
        """将成员移出分组"""
        self._check_member_type(member_type)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM org_group_members "
                    "WHERE group_id=%s AND member_type=%s AND member_id=%s",
                    (group_id, member_type, str(member_id)),
                )
                await conn.commit()
        return True


# Global service instance (pool will be set during startup)
group_service = GroupService()
