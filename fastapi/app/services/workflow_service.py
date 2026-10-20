"""
命令工作流服务

- 工作流 CRUD（command_workflows）
- 节点 CRUD（workflow_nodes）
- 执行日志（workflow_runs / workflow_run_nodes，P4 调试体系）
"""
import json
import logging
from typing import Any, Dict, List, Optional

import aiomysql

from ..models.workflow import (
    END_TARGET,
    NODE_TYPES,
    NODE_TYPE_NAMES,
    RESERVED_NODE_NAMES,
    WorkflowCreateRequest,
    WorkflowInfo,
    WorkflowNodeRequest,
)

logger = logging.getLogger(__name__)


class WorkflowService:
    """命令工作流管理服务"""

    def __init__(self):
        self._pool: Optional[aiomysql.Pool] = None

    def set_pool(self, pool: aiomysql.Pool):
        self._pool = pool

    async def ensure_table(self):
        """创建工作流相关表"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS command_workflows (
                        id          INT AUTO_INCREMENT PRIMARY KEY,
                        name        VARCHAR(100) NOT NULL,
                        description VARCHAR(500) NOT NULL DEFAULT '',
                        enabled     TINYINT NOT NULL DEFAULT 1,
                        is_sensitive TINYINT NOT NULL DEFAULT 0,
                        timeout_sec INT NOT NULL DEFAULT 60,
                        input_schema TEXT,
                        created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_workflow_name (name)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                # 老库幂等补列
                for col in (
                    "input_schema TEXT",
                    "is_sensitive TINYINT NOT NULL DEFAULT 0",
                ):
                    try:
                        await cur.execute(
                            f"ALTER TABLE command_workflows ADD COLUMN {col}"
                        )
                    except Exception:
                        pass  # 列已存在
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS workflow_nodes (
                        id              INT AUTO_INCREMENT PRIMARY KEY,
                        workflow_id     INT NOT NULL,
                        node_type       VARCHAR(30) NOT NULL,
                        name            VARCHAR(100) NOT NULL,
                        params          TEXT,
                        -- sort 已废弃：顺序不再参与运行语义（去向全看下面两个跳转字段），
                        -- 保留列只是不想对线上表做破坏性 DDL；新代码不再读写它。
                        sort            INT NOT NULL DEFAULT 0,
                        next_on_success VARCHAR(100) NOT NULL DEFAULT '',
                        next_on_fail    VARCHAR(100) NOT NULL DEFAULT '',
                        KEY idx_wf_nodes (workflow_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS workflow_runs (
                        id          INT AUTO_INCREMENT PRIMARY KEY,
                        workflow_id INT NOT NULL,
                        robot_id    VARCHAR(10) NOT NULL DEFAULT '',
                        input_json  TEXT,
                        status      VARCHAR(20) NOT NULL DEFAULT 'running',
                        output_json TEXT,
                        error       TEXT,
                        duration_ms INT NOT NULL DEFAULT 0,
                        created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        KEY idx_wf_runs (workflow_id, created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS workflow_run_nodes (
                        id          INT AUTO_INCREMENT PRIMARY KEY,
                        run_id      INT NOT NULL,
                        node_name   VARCHAR(100) NOT NULL,
                        node_type   VARCHAR(30) NOT NULL,
                        status      VARCHAR(20) NOT NULL DEFAULT 'running',
                        input_json  TEXT,
                        output_json TEXT,
                        error       TEXT,
                        duration_ms INT NOT NULL DEFAULT 0,
                        KEY idx_run_nodes (run_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await conn.commit()
        logger.info("✅ command_workflows / workflow_nodes / workflow_runs ensured")

    # ── 工作流 CRUD ─────────────────────────────────────

    async def create_workflow(self, req: WorkflowCreateRequest) -> int:
        name = req.name.strip()
        if not name:
            raise ValueError("工作流名称不能为空")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        """INSERT INTO command_workflows (name, description, timeout_sec, is_sensitive, input_schema)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (
                            name,
                            req.description.strip(),
                            int(req.timeout_sec),
                            1 if getattr(req, "is_sensitive", False) else 0,
                            req.input_schema,
                        ),
                    )
                except aiomysql.IntegrityError:
                    raise ValueError(f"工作流「{name}」已存在")
                wf_id = cur.lastrowid
                await conn.commit()
        logger.info(f"✅ Created workflow #{wf_id}: {name}")
        return wf_id

    async def update_workflow(self, workflow_id: int, req: WorkflowCreateRequest) -> bool:
        name = req.name.strip()
        if not name:
            raise ValueError("工作流名称不能为空")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM command_workflows WHERE id=%s", (workflow_id,))
                if not await cur.fetchone():
                    return False
                await cur.execute(
                    "SELECT id FROM command_workflows WHERE name=%s AND id<>%s",
                    (name, workflow_id),
                )
                if await cur.fetchone():
                    raise ValueError(f"工作流「{name}」已被其他工作流使用")
                await cur.execute(
                    """UPDATE command_workflows
                       SET name=%s, description=%s, timeout_sec=%s, enabled=%s, input_schema=%s
                       WHERE id=%s""",
                    (
                        name,
                        req.description.strip(),
                        int(req.timeout_sec),
                        1,
                        req.input_schema,
                        workflow_id,
                    ),
                )
                await conn.commit()
                affected = cur.rowcount
        return affected > 0

    async def delete_workflow(self, workflow_id: int) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM command_workflows WHERE id=%s", (workflow_id,))
                if not await cur.fetchone():
                    return False
                await cur.execute("DELETE FROM workflow_nodes WHERE workflow_id=%s", (workflow_id,))
                await cur.execute(
                    "DELETE FROM workflow_runs WHERE workflow_id=%s", (workflow_id,)
                )
                await cur.execute(
                    "DELETE FROM org_group_members "
                    "WHERE member_type='workflow' AND member_id=%s",
                    (workflow_id,),
                )
                await cur.execute("DELETE FROM command_workflows WHERE id=%s", (workflow_id,))
                await conn.commit()
        logger.info(f"🗑 Deleted workflow #{workflow_id}")
        return True

    async def set_enabled(self, workflow_id: int, enabled: bool) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE command_workflows SET enabled=%s WHERE id=%s",
                    (1 if enabled else 0, workflow_id),
                )
                await conn.commit()
                affected = cur.rowcount
        return affected > 0

    async def set_sensitive(self, workflow_id: int, sensitive: bool) -> bool:
        """设置敏感标记（高风险操作，仅提示与审计，不强制拦截）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE command_workflows SET is_sensitive=%s WHERE id=%s",
                    (1 if sensitive else 0, workflow_id),
                )
                await conn.commit()
        return True

    async def update_input_schema(self, workflow_id: int, input_schema: Optional[str]) -> bool:
        """保存工作流输入参数声明（规范化 JSON 字符串或 None）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM command_workflows WHERE id=%s", (workflow_id,))
                if not await cur.fetchone():
                    return False
                await cur.execute(
                    "UPDATE command_workflows SET input_schema=%s WHERE id=%s",
                    (input_schema, workflow_id),
                )
                await conn.commit()
        return True

    async def list_workflows(self) -> List[WorkflowInfo]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT w.id, w.name, w.description, w.enabled, w.is_sensitive, w.timeout_sec,
                              w.created_at, w.input_schema,
                              (SELECT COUNT(*) FROM workflow_nodes n WHERE n.workflow_id=w.id)
                       FROM command_workflows w ORDER BY w.id ASC"""
                )
                rows = await cur.fetchall()
        return [
            WorkflowInfo(
                id=r[0],
                name=r[1],
                description=r[2] or "",
                enabled=bool(r[3]),
                is_sensitive=bool(r[4]),
                timeout_sec=r[5],
                created_at=r[6].isoformat() if r[6] else "",
                input_schema=r[7],
                node_count=r[8] or 0,
            )
            for r in rows
        ]

    async def get_workflow(self, workflow_id: int) -> Optional[dict]:
        """工作流详情（含节点列表）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT w.id, w.name, w.description, w.enabled, w.is_sensitive, w.timeout_sec,
                              w.created_at, w.input_schema
                       FROM command_workflows w WHERE w.id=%s""",
                    (workflow_id,),
                )
                row = await cur.fetchone()
                if not row:
                    return None
                # 按 id 排（= 创建顺序）。顺序只影响画布上"谁是第一个节点"，
                # 运行语义完全由各节点自己写的跳转决定。
                await cur.execute(
                    """SELECT id, node_type, name, params, next_on_success, next_on_fail
                       FROM workflow_nodes WHERE workflow_id=%s ORDER BY id ASC""",
                    (workflow_id,),
                )
                nrows = await cur.fetchall()
        return {
            "id": row[0],
            "name": row[1],
            "description": row[2] or "",
            "enabled": bool(row[3]),
            "is_sensitive": bool(row[4]),
            "timeout_sec": row[5],
            "created_at": row[6].isoformat() if row[6] else "",
            "input_schema": row[7],
            "nodes": [
                {
                    "id": n[0],
                    "node_type": n[1],
                    "name": n[2],
                    "params": _safe_json(n[3]),
                    "next_on_success": n[4] or "",
                    "next_on_fail": n[5] or "",
                }
                for n in nrows
            ],
        }

    # ── 节点 CRUD ───────────────────────────────────────

    async def _node_name_exists(
        self, workflow_id: int, name: str, exclude_id: Optional[int] = None
    ) -> bool:
        """节点名是否已被占用（exclude_id 用于编辑时排除自身）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                if exclude_id:
                    await cur.execute(
                        "SELECT 1 FROM workflow_nodes WHERE workflow_id=%s AND name=%s AND id<>%s",
                        (workflow_id, name, exclude_id),
                    )
                else:
                    await cur.execute(
                        "SELECT 1 FROM workflow_nodes WHERE workflow_id=%s AND name=%s",
                        (workflow_id, name),
                    )
                return bool(await cur.fetchone())

    async def add_node(
        self,
        workflow_id: int,
        req: WorkflowNodeRequest,
        link_terminal_tail: bool = False,
    ) -> int:
        """新建节点（放在列表里最后）。去向完全由调用方传入的 next_on_success 决定。

        link_terminal_tail=True 时，若列表里最后那个节点是"到此结束"（END_TARGET），
        把它的成功跳转改成指向新节点——否则追加出来的节点谁也到不了。
        """
        if req.node_type not in NODE_TYPE_NAMES:
            raise ValueError(f"无效的节点类型: {req.node_type}")
        if req.name.strip() in RESERVED_NODE_NAMES:
            raise ValueError(f"节点名「{req.name.strip()}」是保留字，请换一个")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM command_workflows WHERE id=%s", (workflow_id,))
                if not await cur.fetchone():
                    raise ValueError("工作流不存在")
                await cur.execute(
                    "SELECT id, next_on_success FROM workflow_nodes "
                    "WHERE workflow_id=%s ORDER BY id DESC LIMIT 1",
                    (workflow_id,),
                )
                tail = await cur.fetchone()
                # 节点名：留空自动生成（node_1/node_2/… 取第一个没被占用的），
                # 且保证唯一——跳转靠节点名，重名会导致流程错乱
                name = req.name.strip()
                if not name:
                    k = 1
                    while await self._node_name_exists(workflow_id, f"node_{k}"):
                        k += 1
                    name = f"node_{k}"
                else:
                    base, n = name, 1
                    while await self._node_name_exists(workflow_id, name):
                        n += 1
                        name = f"{base}_{n}"
                await cur.execute(
                    """INSERT INTO workflow_nodes
                           (workflow_id, node_type, name, params, next_on_success, next_on_fail)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (
                        workflow_id,
                        req.node_type,
                        name,
                        json.dumps(req.params, ensure_ascii=False),
                        req.next_on_success,
                        req.next_on_fail,
                    ),
                )
                node_id = cur.lastrowid
                if link_terminal_tail and tail and (tail[1] or "").strip() == END_TARGET:
                    await cur.execute(
                        "UPDATE workflow_nodes SET next_on_success=%s WHERE id=%s",
                        (name, tail[0]),
                    )
                await conn.commit()
        return node_id

    async def insert_node_after(
        self, workflow_id: int, after_node_id: int, req: WorkflowNodeRequest
    ) -> int:
        """在 after_node_id **后面**接一个新节点：源节点 → 新节点 → 源节点原来的去向。

        这是纯接线操作，不涉及位置：新节点在列表里排最后（id 最大），画布按连线自动布局。
        - 源节点原来是"到此结束" → 新节点也结束（链尾还是链尾）
        - 源节点原来显式指向 Z → 新节点指向 Z，Z 不会因为插队而失联
        找不到 after_node_id 时退化为普通新建。
        """
        if req.node_type not in NODE_TYPE_NAMES:
            raise ValueError(f"无效的节点类型: {req.node_type}")
        if req.name.strip() in RESERVED_NODE_NAMES:
            raise ValueError(f"节点名「{req.name.strip()}」是保留字，请换一个")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT node_type, next_on_success FROM workflow_nodes "
                    "WHERE id=%s AND workflow_id=%s",
                    (after_node_id, workflow_id),
                )
                row = await cur.fetchone()
        if not row:
            return await self.add_node(workflow_id, req)
        if row[0] == "switch":
            # switch 的去向全在各分支上，它自己没有"下一个"。硬插在后面只会得到一个
            # 谁都不指向的孤儿节点（引擎也不会走它）。
            raise ValueError(
                "多分支判断节点没有「下一个」——请点它某条分支线上的「＋」给那条分支挂节点"
            )
        inherited = (row[1] or "").strip()
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                name = req.name.strip()
                if not name:
                    k = 1
                    while await self._node_name_exists(workflow_id, f"node_{k}"):
                        k += 1
                    name = f"node_{k}"
                else:
                    base, n = name, 1
                    while await self._node_name_exists(workflow_id, name):
                        n += 1
                        name = f"{base}_{n}"
                await cur.execute(
                    """INSERT INTO workflow_nodes
                           (workflow_id, node_type, name, params, next_on_success, next_on_fail)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (
                        workflow_id,
                        req.node_type,
                        name,
                        json.dumps(req.params, ensure_ascii=False),
                        inherited,
                        req.next_on_fail,
                    ),
                )
                node_id = cur.lastrowid
                await cur.execute(
                    "UPDATE workflow_nodes SET next_on_success=%s WHERE id=%s",
                    (name, after_node_id),
                )
                await conn.commit()
        return node_id

    async def update_node(self, node_id: int, req: WorkflowNodeRequest) -> bool:
        if req.node_type not in NODE_TYPE_NAMES:
            raise ValueError(f"无效的节点类型: {req.node_type}")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT workflow_id, name FROM workflow_nodes WHERE id=%s", (node_id,)
                )
                row = await cur.fetchone()
                if not row:
                    return False
                workflow_id, old_name = row[0], row[1]
                name = req.name.strip() or old_name  # 名称留空则保留原名
                if name in RESERVED_NODE_NAMES:
                    raise ValueError(f"节点名「{name}」是保留字，请换一个")
                if name != old_name and await self._node_name_exists(
                    workflow_id, name, exclude_id=node_id
                ):
                    raise ValueError(f"节点名「{name}」已被其他节点使用")
                await cur.execute(
                    """UPDATE workflow_nodes SET node_type=%s, name=%s, params=%s,
                                                 next_on_success=%s, next_on_fail=%s
                       WHERE id=%s""",
                    (
                        req.node_type,
                        name,
                        json.dumps(req.params, ensure_ascii=False),
                        req.next_on_success,
                        req.next_on_fail,
                        node_id,
                    ),
                )
                await conn.commit()
        return True

    async def delete_node(self, node_id: int) -> bool:
        """删除节点，并清掉其它节点里指向它的跳转。

        不清引用的话，switch 分支会一直指着一个不存在的节点名：画布上变成"死引用"，
        运行到那儿直接报「跳转目标不存在」中止，用户还得手工去下拉里改掉。

        **起点节点（id 最小的那个）不允许删**：引擎就是从它开始跑的，删掉之后起点会
        悄悄换成别的节点，整条流程的入口就变了。起点目前没有独立的标记，就是"第一个
        创建的节点"，所以禁止删除是唯一能保证入口稳定的办法。要重建请删整个工作流。
        """
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT workflow_id, name FROM workflow_nodes WHERE id=%s", (node_id,)
                )
                row = await cur.fetchone()
                if not row:
                    return False
                workflow_id, name = row[0], row[1]
                await cur.execute(
                    "SELECT id FROM workflow_nodes WHERE workflow_id=%s ORDER BY id ASC LIMIT 1",
                    (workflow_id,),
                )
                first = await cur.fetchone()
                if first and first[0] == node_id:
                    raise ValueError(
                        "起点节点不能删除（工作流从这里开始跑）。"
                        "要换起点请新建工作流，或把起点节点改成你要的第一步。"
                    )
                await cur.execute("DELETE FROM workflow_nodes WHERE id=%s", (node_id,))
                affected = cur.rowcount
                if affected and name:
                    await cur.execute(
                        """SELECT id, params, next_on_success, next_on_fail
                           FROM workflow_nodes WHERE workflow_id=%s""",
                        (workflow_id,),
                    )
                    refs = await cur.fetchall()
                    for rid, raw_params, ns, nf in refs:
                        params = _safe_json(raw_params)
                        if not isinstance(params, dict):
                            params = {}
                        changed = False
                        branches = params.get("branches")
                        if isinstance(branches, list):
                            for b in branches:
                                if isinstance(b, dict) and str(b.get("next") or "") == name:
                                    b["next"] = ""
                                    changed = True
                        if str(params.get("default_next") or "") == name:
                            params["default_next"] = ""
                            changed = True
                        if ns == name:
                            ns, changed = "", True
                        if nf == name:
                            nf, changed = "", True
                        if changed:
                            await cur.execute(
                                """UPDATE workflow_nodes
                                   SET params=%s, next_on_success=%s, next_on_fail=%s
                                   WHERE id=%s""",
                                (
                                    json.dumps(params, ensure_ascii=False),
                                    ns or "",
                                    nf or "",
                                    rid,
                                ),
                            )
                await conn.commit()
        return affected > 0

    # ── 执行日志 ────────────────────────────────────────

    async def create_run(self, workflow_id: int, robot_id: str, input_ctx: dict) -> int:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO workflow_runs (workflow_id, robot_id, input_json)
                       VALUES (%s,%s,%s)""",
                    (workflow_id, robot_id, json.dumps(input_ctx, ensure_ascii=False)),
                )
                run_id = cur.lastrowid
                await conn.commit()
        return run_id

    async def finish_run(self, run_id: int, status: str, output: dict, error: str, duration_ms: int):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """UPDATE workflow_runs SET status=%s, output_json=%s, error=%s, duration_ms=%s
                       WHERE id=%s""",
                    (
                        status,
                        json.dumps(output, ensure_ascii=False, default=str) if output else None,
                        error or None,
                        int(duration_ms),
                        run_id,
                    ),
                )
                await conn.commit()

    async def log_node(
        self,
        run_id: int,
        node_name: str,
        node_type: str,
        status: str,
        input_data: dict,
        output_data: dict,
        error: str,
        duration_ms: int,
    ):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO workflow_run_nodes
                           (run_id, node_name, node_type, status, input_json, output_json, error, duration_ms)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        run_id,
                        node_name,
                        node_type,
                        status,
                        json.dumps(input_data, ensure_ascii=False, default=str),
                        json.dumps(output_data, ensure_ascii=False, default=str)
                        if output_data
                        else None,
                        error or None,
                        int(duration_ms),
                    ),
                )
                await conn.commit()

    async def list_runs(self, workflow_id: int, limit: int = 30) -> List[dict]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT id, workflow_id, robot_id, status, duration_ms, created_at
                       FROM workflow_runs WHERE workflow_id=%s
                       ORDER BY id DESC LIMIT %s""",
                    (workflow_id, int(limit)),
                )
                rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "workflow_id": r[1],
                "robot_id": r[2],
                "status": r[3],
                "duration_ms": r[4],
                "created_at": r[5].isoformat() if r[5] else "",
            }
            for r in rows
        ]

    async def get_run(self, run_id: int) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, workflow_id, robot_id, status, output_json, error, duration_ms, created_at "
                    "FROM workflow_runs WHERE id=%s",
                    (run_id,),
                )
                row = await cur.fetchone()
                if not row:
                    return None
                await cur.execute(
                    """SELECT node_name, node_type, status, input_json, output_json, error, duration_ms
                       FROM workflow_run_nodes WHERE run_id=%s ORDER BY id ASC""",
                    (run_id,),
                )
                nrows = await cur.fetchall()
        return {
            "id": row[0],
            "workflow_id": row[1],
            "robot_id": row[2],
            "status": row[3],
            "output": _safe_json(row[4]),
            "error": row[5],
            "duration_ms": row[6],
            "created_at": row[7].isoformat() if row[7] else "",
            "nodes": [
                {
                    "node_name": n[0],
                    "node_type": n[1],
                    "status": n[2],
                    "input": _safe_json(n[3]),
                    "output": _safe_json(n[4]),
                    "error": n[5],
                    "duration_ms": n[6],
                }
                for n in nrows
            ],
        }


def _safe_json(s) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return s


# 全局服务实例
workflow_service = WorkflowService()
