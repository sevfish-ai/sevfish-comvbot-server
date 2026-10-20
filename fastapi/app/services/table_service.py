"""
表格（自定义数据表）服务

- 每张用户表格 = 一张真实 MySQL 表 t_tbl_{id}（动态 DDL，字段由元数据表决定）
- 支持字段类型：varchar / int / float / datetime / text / json
- 授权给智能体：agent_table_grants（0=只读 1=读写），供 LLM 工具与工作流节点复用
- 所有动态 SQL 均使用参数化查询 + 表名/字段名白名单校验（防注入）
"""
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiomysql

from ..models.table import (
    FIELD_TYPES,
    FieldDef,
    TableCreateRequest,
    TableInfo,
)

logger = logging.getLogger(__name__)

# 物理表前缀（防注入：表名/字段名一律白名单校验）
_TABLE_PREFIX = "t_tbl_"
_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

_FIELD_TYPE_DDL = {
    "varchar": "VARCHAR(255)",
    "int": "INT",
    "float": "FLOAT",
    "datetime": "DATETIME",
    "text": "TEXT",
    "json": "JSON",
}

# 查询条件允许的操作符
_WHERE_OPS = {"=", "!=", ">", "<", ">=", "<=", "like"}


class TableService:
    """表格管理服务（MySQL 存储）"""

    def __init__(self):
        self._pool: Optional[aiomysql.Pool] = None

    def set_pool(self, pool: aiomysql.Pool):
        self._pool = pool

    # ── 建表 ────────────────────────────────────────────

    async def ensure_table(self):
        """创建元数据表 custom_tables / custom_table_fields / agent_table_grants"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS custom_tables (
                        id          INT AUTO_INCREMENT PRIMARY KEY,
                        name        VARCHAR(100) NOT NULL,
                        table_key   VARCHAR(64)  NOT NULL DEFAULT '',
                        description VARCHAR(500) NOT NULL DEFAULT '',
                        created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_custom_table_name (name),
                        UNIQUE KEY uk_custom_table_key (table_key)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS custom_table_fields (
                        id          INT AUTO_INCREMENT PRIMARY KEY,
                        table_id    INT          NOT NULL,
                        field_name  VARCHAR(64)  NOT NULL,
                        field_type  VARCHAR(20)  NOT NULL DEFAULT 'varchar',
                        field_label VARCHAR(100) NOT NULL DEFAULT '',
                        required    TINYINT      NOT NULL DEFAULT 0,
                        sort        INT          NOT NULL DEFAULT 0,
                        UNIQUE KEY uk_field (table_id, field_name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_table_grants (
                        agent_id   INT NOT NULL,
                        table_id   INT NOT NULL,
                        permission TINYINT NOT NULL DEFAULT 0,
                        PRIMARY KEY (agent_id, table_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS table_templates (
                        id          INT AUTO_INCREMENT PRIMARY KEY,
                        name        VARCHAR(100) NOT NULL,
                        description VARCHAR(500) NOT NULL DEFAULT '',
                        fields_json TEXT NOT NULL,
                        created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_table_template_name (name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await conn.commit()
        logger.info("✅ custom_tables / custom_table_fields / agent_table_grants / table_templates ensured")

    # ── 校验辅助 ────────────────────────────────────────

    @staticmethod
    def _validate_name(name: str, what: str = "名称"):
        if not _NAME_RE.match(name):
            raise ValueError(f"{what}不合法：必须字母开头，仅含字母/数字/下划线（收到：{name}）")

    @staticmethod
    def _physical_table(table_key: str) -> str:
        """table_key（tbl_12）→ 物理表名（t_tbl_12），并校验合法性"""
        if not table_key or not table_key.startswith("tbl_"):
            raise ValueError(f"无效的表格标识: {table_key}")
        TableService._validate_name(table_key, "表格标识")
        return f"{_TABLE_PREFIX}{table_key[4:]}"

    @staticmethod
    def _field_ddl(f: dict) -> str:
        ftype = str(f.get("type") or "varchar")
        if ftype not in _FIELD_TYPE_DDL:
            raise ValueError(f"不支持的字段类型: {ftype}（支持 {FIELD_TYPES}）")
        required = "NOT NULL" if f.get("required") else "DEFAULT NULL"
        return f"{_FIELD_TYPE_DDL[ftype]} {required}"

    @staticmethod
    def _normalize_where(where) -> List[Tuple[str, str, Any]]:
        """
        归一化查询条件为 [(field, op, value), ...]
        支持两种输入：
          dict:  {field: value}（全部等值）
          list:  [{"field","op","value"}]
        """
        conds: List[Tuple[str, str, Any]] = []
        if not where:
            return conds
        if isinstance(where, dict):
            for f, v in where.items():
                TableService._validate_name(str(f), "字段名")
                conds.append((str(f), "=", v))
            return conds
        if isinstance(where, list):
            for item in where:
                if not isinstance(item, dict):
                    continue
                f = str(item.get("field") or "")
                op = str(item.get("op") or "=")
                v = item.get("value")
                TableService._validate_name(f, "字段名")
                if op not in _WHERE_OPS:
                    raise ValueError(f"不支持的条件操作符: {op}")
                conds.append((f, op, v))
            return conds
        return conds

    # ── 表格元数据 CRUD ─────────────────────────────────

    async def create_table(self, req: TableCreateRequest) -> int:
        """创建表格（元数据 + 物理表），返回表格 id"""
        name = req.name.strip()
        if not name:
            raise ValueError("表格名称不能为空")
        fields = req.fields or []
        if not fields:
            raise ValueError("至少需要一个字段")
        seen = set()
        for f in fields:
            fname = str(f.get("name") or "").strip()
            self._validate_name(fname, f"字段名")
            if fname in seen:
                raise ValueError(f"字段名重复: {fname}")
            seen.add(fname)
            ftype = str(f.get("type") or "varchar")
            if ftype not in _FIELD_TYPE_DDL:
                raise ValueError(f"不支持的字段类型: {ftype}")

        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "INSERT INTO custom_tables (name, description) VALUES (%s, %s)",
                        (name, req.description.strip()),
                    )
                except aiomysql.IntegrityError:
                    raise ValueError(f"表格「{name}」已存在")
                table_id = cur.lastrowid
                table_key = f"tbl_{table_id}"
                await cur.execute(
                    "UPDATE custom_tables SET table_key=%s WHERE id=%s",
                    (table_key, table_id),
                )
                for i, f in enumerate(fields):
                    await cur.execute(
                        """INSERT INTO custom_table_fields
                               (table_id, field_name, field_type, field_label, required, sort)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (
                            table_id,
                            str(f["name"]).strip(),
                            str(f.get("type") or "varchar"),
                            str(f.get("label") or "").strip(),
                            1 if f.get("required") else 0,
                            i,
                        ),
                    )
                ddl = ",\n".join(
                    f"`{str(f['name']).strip()}` {self._field_ddl(f)}"
                    for f in fields
                )
                await cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS `{_TABLE_PREFIX}{table_id}` (
                        `id` INT AUTO_INCREMENT PRIMARY KEY,
                        {ddl},
                        `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await conn.commit()
        logger.info(f"✅ Created table: {name} (id={table_id}, fields={len(fields)})")
        return table_id

    async def list_tables(self) -> List[TableInfo]:
        """列出全部表格（含字段数、行数、授权数）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT c.id, c.name, c.table_key, c.description, c.created_at,
                              (SELECT COUNT(*) FROM custom_table_fields f WHERE f.table_id=c.id) AS fc,
                              (SELECT COUNT(*) FROM agent_table_grants g WHERE g.table_id=c.id) AS gc
                       FROM custom_tables c ORDER BY c.id DESC"""
                )
                rows = await cur.fetchall()
        tables = []
        for r in rows:
            row_count = 0
            try:
                async with self._pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            f"SELECT COUNT(*) FROM `{_TABLE_PREFIX}{r[0]}`"
                        )
                        row_count = (await cur.fetchone())[0]
            except Exception:
                row_count = 0
            tables.append(
                TableInfo(
                    id=r[0],
                    name=r[1],
                    table_key=r[2],
                    description=r[3] or "",
                    created_at=r[4].isoformat() if r[4] else "",
                    field_count=r[5] or 0,
                    row_count=row_count,
                    grant_count=r[6] or 0,
                )
            )
        return tables

    async def get_table(self, table_id: int) -> Optional[dict]:
        """获取表格详情（含字段定义列表）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, name, table_key, description, created_at FROM custom_tables WHERE id=%s",
                    (table_id,),
                )
                row = await cur.fetchone()
                if not row:
                    return None
                await cur.execute(
                    """SELECT id, field_name, field_type, field_label, required, sort
                       FROM custom_table_fields WHERE table_id=%s ORDER BY sort ASC, id ASC""",
                    (table_id,),
                )
                frows = await cur.fetchall()
        return {
            "id": row[0],
            "name": row[1],
            "table_key": row[2],
            "description": row[3] or "",
            "created_at": row[4].isoformat() if row[4] else "",
            "fields": [
                {
                    "id": f[0],
                    "name": f[1],
                    "type": f[2],
                    "label": f[3] or "",
                    "required": bool(f[4]),
                }
                for f in frows
            ],
        }

    async def get_table_by_key(self, table_key: str) -> Optional[dict]:
        """按 table_key（如 tbl_12）获取表格详情"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM custom_tables WHERE table_key=%s", (table_key,)
                )
                row = await cur.fetchone()
        if not row:
            return None
        return await self.get_table(row[0])

    async def delete_table(self, table_id: int) -> bool:
        """删除表格：先删授权/字段/元数据，再 DROP 物理表"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT table_key FROM custom_tables WHERE id=%s", (table_id,))
                row = await cur.fetchone()
                if not row:
                    return False
                table_key = row[0]
                await cur.execute(
                    "DELETE FROM agent_table_grants WHERE table_id=%s", (table_id,)
                )
                await cur.execute(
                    "DELETE FROM custom_table_fields WHERE table_id=%s", (table_id,)
                )
                await cur.execute(
                    "DELETE FROM org_group_members "
                    "WHERE member_type='table' AND member_id=%s",
                    (table_id,),
                )
                await cur.execute(
                    "DELETE FROM custom_tables WHERE id=%s", (table_id,)
                )
                try:
                    await cur.execute(f"DROP TABLE IF EXISTS `{_TABLE_PREFIX}{table_id}`")
                except Exception as e:
                    logger.warning(f"DROP TABLE failed {_TABLE_PREFIX}{table_id}: {e}")
                await conn.commit()
        logger.info(f"🗑 Deleted table #{table_id} ({table_key})")
        return True

    # ── 表格模板（快速创建表格）─────────────────────────

    async def create_template(
        self, name: str, description: str = "", fields: List[dict] = None
    ) -> int:
        """保存表格模板（字段定义以 JSON 存储），返回模板 id"""
        name = (name or "").strip()
        if not name:
            raise ValueError("模板名称不能为空")
        fields = fields or []
        if not fields:
            raise ValueError("至少需要一个字段")
        # 轻量校验字段结构
        for f in fields:
            fname = str(f.get("name") or "").strip()
            if not fname:
                raise ValueError("模板字段名不能为空")
            if str(f.get("type") or "varchar") not in _FIELD_TYPE_DDL:
                raise ValueError(f"不支持的字段类型: {f.get('type')}")
        fields_json = json.dumps(fields, ensure_ascii=False)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "INSERT INTO table_templates (name, description, fields_json) VALUES (%s, %s, %s)",
                        (name, (description or "").strip(), fields_json),
                    )
                    await conn.commit()
                    return cur.lastrowid
                except aiomysql.IntegrityError as e:
                    raise ValueError(f"模板「{name}」已存在") from e

    async def list_templates(self) -> List[dict]:
        """列出全部模板：[{id,name,description,field_count,fields,created_at}]"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, name, description, fields_json, created_at "
                    "FROM table_templates ORDER BY id ASC"
                )
                rows = await cur.fetchall()
        result = []
        for r in rows:
            try:
                fields = json.loads(r[3] or "[]")
            except Exception:
                fields = []
            result.append(
                {
                    "id": r[0],
                    "name": r[1],
                    "description": r[2] or "",
                    "field_count": len(fields),
                    "fields": fields,
                    "created_at": r[4].isoformat() if r[4] else "",
                }
            )
        return result

    async def get_template(self, template_id: int) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, name, description, fields_json, created_at "
                    "FROM table_templates WHERE id=%s",
                    (template_id,),
                )
                r = await cur.fetchone()
        if not r:
            return None
        try:
            fields = json.loads(r[3] or "[]")
        except Exception:
            fields = []
        return {
            "id": r[0],
            "name": r[1],
            "description": r[2] or "",
            "fields": fields,
            "created_at": r[4].isoformat() if r[4] else "",
        }

    async def delete_template(self, template_id: int) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM table_templates WHERE id=%s", (template_id,)
                )
                await conn.commit()
                return cur.rowcount > 0

    # ── 字段管理 ────────────────────────────────────────

    async def add_field(self, table_id: int, fd: FieldDef) -> bool:
        """新增字段（元数据 + ALTER TABLE）"""
        self._validate_name(fd.name, "字段名")
        if fd.type not in _FIELD_TYPE_DDL:
            raise ValueError(f"不支持的字段类型: {fd.type}")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM custom_tables WHERE id=%s", (table_id,))
                if not await cur.fetchone():
                    return False
                try:
                    await cur.execute(
                        """INSERT INTO custom_table_fields
                               (table_id, field_name, field_type, field_label, required, sort)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (table_id, fd.name, fd.type, fd.label, 1 if fd.required else 0, 999),
                    )
                except aiomysql.IntegrityError:
                    raise ValueError(f"字段「{fd.name}」已存在")
                await cur.execute(
                    f"ALTER TABLE `{_TABLE_PREFIX}{table_id}` "
                    f"ADD COLUMN `{fd.name}` {self._field_ddl(fd.model_dump())}"
                )
                await conn.commit()
        logger.info(f"✅ Added field {fd.name} to table #{table_id}")
        return True

    async def remove_field(self, table_id: int, field_id: int) -> bool:
        """删除字段（元数据 + ALTER TABLE DROP COLUMN）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT field_name FROM custom_table_fields WHERE id=%s AND table_id=%s",
                    (field_id, table_id),
                )
                row = await cur.fetchone()
                if not row:
                    return False
                await cur.execute(
                    "DELETE FROM custom_table_fields WHERE id=%s", (field_id,)
                )
                await cur.execute(
                    f"ALTER TABLE `{_TABLE_PREFIX}{table_id}` DROP COLUMN `{row[0]}`"
                )
                await conn.commit()
        logger.info(f"🗑 Removed field {row[0]} from table #{table_id}")
        return True

    # ── 数据读写（参数化 SQL + 字段白名单）──────────────

    async def _field_names(self, table_id: int) -> set:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT field_name FROM custom_table_fields WHERE table_id=%s",
                    (table_id,),
                )
                rows = await cur.fetchall()
        return {r[0] for r in rows}

    async def query_rows(
        self,
        table_key: str,
        where=None,
        limit: int = 50,
        order_by: str = "id",
        desc: bool = True,
    ) -> List[dict]:
        """查询数据行。where 支持 {field:value} 或 [{"field","op","value"}]"""
        table = await self.get_table_by_key(table_key)
        if not table:
            raise ValueError(f"表格不存在: {table_key}")
        conds = self._normalize_where(where)
        sql = f"SELECT * FROM `{_TABLE_PREFIX}{table['id']}`"
        params: List[Any] = []
        if conds:
            sql += " WHERE " + " AND ".join(
                f"`{f}` {op} %s" for f, op, _ in conds
            )
            params = [v for _, _, v in conds]
        sql += f" ORDER BY `{order_by}` {'DESC' if desc else 'ASC'} LIMIT %s"
        params.append(int(limit))
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.cursors.DictCursor) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        # json 字段序列化
        fmap = {f["name"]: f["type"] for f in table["fields"]}
        for r in rows:
            for k, v in r.items():
                if fmap.get(k) == "json" and isinstance(v, str):
                    try:
                        r[k] = json.loads(v)
                    except Exception:
                        pass
        return rows

    async def insert_row(self, table_key: str, data: dict) -> int:
        """插入一行，返回新行 id"""
        table = await self.get_table_by_key(table_key)
        if not table:
            raise ValueError(f"表格不存在: {table_key}")
        field_names = await self._field_names(table["id"])
        clean = {}
        for k, v in (data or {}).items():
            if k in field_names:
                clean[k] = self._to_sql_value(v)
        if not clean:
            raise ValueError("没有可写入的字段数据")
        cols = ", ".join(f"`{k}`" for k in clean)
        placeholders = ", ".join(["%s"] * len(clean))
        sql = f"INSERT INTO `{_TABLE_PREFIX}{table['id']}` ({cols}) VALUES ({placeholders})"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, list(clean.values()))
                new_id = cur.lastrowid
                await conn.commit()
        return new_id

    async def update_row(self, table_key: str, row_id: int, data: dict) -> bool:
        """按 id 更新行"""
        table = await self.get_table_by_key(table_key)
        if not table:
            raise ValueError(f"表格不存在: {table_key}")
        field_names = await self._field_names(table["id"])
        clean = {k: self._to_sql_value(v) for k, v in (data or {}).items() if k in field_names}
        if not clean:
            return False
        sets = ", ".join(f"`{k}`=%s" for k in clean)
        sql = f"UPDATE `{_TABLE_PREFIX}{table['id']}` SET {sets} WHERE id=%s"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, list(clean.values()) + [int(row_id)])
                await conn.commit()
                affected = cur.rowcount
        return affected > 0

    async def delete_row(self, table_key: str, row_id: int) -> bool:
        table = await self.get_table_by_key(table_key)
        if not table:
            raise ValueError(f"表格不存在: {table_key}")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM `{_TABLE_PREFIX}{table['id']}` WHERE id=%s", (int(row_id),)
                )
                await conn.commit()
                affected = cur.rowcount
        return affected > 0

    @staticmethod
    def _to_sql_value(v):
        """json 字段 / 布尔等转可存储类型"""
        if isinstance(v, bool):
            return 1 if v else 0
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, datetime):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        return v

    # ── 授权管理 ────────────────────────────────────────

    async def set_agent_grants(self, table_id: int, grants: Dict[int, int]):
        """整表覆盖授权：{agent_id: permission(0/1)}"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM agent_table_grants WHERE table_id=%s", (table_id,)
                )
                for agent_id, perm in (grants or {}).items():
                    await cur.execute(
                        """INSERT INTO agent_table_grants (agent_id, table_id, permission)
                           VALUES (%s,%s,%s)""",
                        (int(agent_id), table_id, 1 if int(perm) else 0),
                    )
                await conn.commit()
        logger.info(f"🔑 Set grants for table #{table_id}: {grants}")

    async def get_agent_grants(self, table_id: int) -> Dict[int, int]:
        """返回 {agent_id: permission}"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT agent_id, permission FROM agent_table_grants WHERE table_id=%s",
                    (table_id,),
                )
                rows = await cur.fetchall()
        return {r[0]: r[1] for r in rows}

    async def get_tables_of_agent(self, agent_id: int) -> List[dict]:
        """智能体已授权的表格列表：[{id, name, table_key, permission}]"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT t.id, t.name, t.table_key, g.permission
                       FROM agent_table_grants g
                       JOIN custom_tables t ON t.id = g.table_id
                       WHERE g.agent_id=%s ORDER BY t.id ASC""",
                    (agent_id,),
                )
                rows = await cur.fetchall()
        return [
            {"id": r[0], "name": r[1], "table_key": r[2], "permission": r[3]}
            for r in rows
        ]

    async def resolve_table_key(self, ident: str) -> Optional[str]:
        """表格标识归一化：table_key（tbl_12）或表格名称 → table_key；找不到返回 None。
        名称有唯一约束，LLM 工具调用常传名称而非内部 table_key，统一在此解析。"""
        ident = (ident or "").strip()
        if not ident:
            return None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT table_key FROM custom_tables WHERE table_key=%s OR name=%s LIMIT 1",
                    (ident, ident),
                )
                row = await cur.fetchone()
        return row[0] if row else None

    async def check_permission(
        self, agent_id: int, table_key: str, need_write: bool = False
    ) -> bool:
        """校验智能体对某表的权限。need_write=True 时要求 1（读写）。"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT g.permission FROM agent_table_grants g
                       JOIN custom_tables t ON t.id = g.table_id
                       WHERE g.agent_id=%s AND t.table_key=%s""",
                    (agent_id, table_key),
                )
                row = await cur.fetchone()
        if not row:
            return False
        perm = int(row[0])
        if need_write:
            return perm >= 1
        return True  # 有任何授权（0=只读 或 1=读写）即允许读


# 全局服务实例（pool 在 startup 时注入）
table_service = TableService()
