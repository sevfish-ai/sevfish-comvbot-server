"""
Robot registration and management service (MySQL backed)
"""

import json
import uuid
import random
import hashlib
import logging
from typing import List, Optional, Tuple
from datetime import datetime

import aiomysql

from ..models.robot import RobotInfo, ReplyStrategy, RobotStatus


logger = logging.getLogger(__name__)


# ── v2 策略配置常量 ───────────────────────────────────────
# 四类聊天室（room_type: "1"=外部群 "2"=外部私聊 "3"=内部群 "4"=内部私聊）
# 私聊模式：reply / silent；群聊模式：track_at / at_only / reply_all / silent
DEFAULT_CONFIG = {
    "1": {"mode": "at_only", "merge_window": 4, "track_duration": 180},  # 外部群
    "2": {"mode": "reply", "merge_window": 4},                            # 外部私聊
    "3": {"mode": "at_only", "merge_window": 4, "track_duration": 180},  # 内部群
    "4": {"mode": "reply", "merge_window": 4},                            # 内部私聊
}
DEFAULT_MAX_CONCURRENT_LLM = 2
DEFAULT_MAX_CALLS_PER_MIN = 5

PRIVATE_MODES = ("reply", "silent")
GROUP_MODES = ("track_at", "at_only", "reply_all", "silent")


def normalize_config(config: dict) -> dict:
    """合并用户输入与默认配置并校验：缺失的聊天室类型用默认值补齐，
    非法 mode / 窗口值回退默认。返回完整四类配置 + 顶层成员黑名单。

    顶层键 member_blocklist：策略组全局成员黑名单（精确匹配群聊发言者名），
    命中该名单的成员消息不入队、不调 LLM。为空列表 = 不限制。"""
    out = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝默认
    if not isinstance(config, dict):
        out["member_blocklist"] = []
        return out
    # 顶层成员黑名单：list 原样归一；字符串按逗号拆分；非法值回退空列表
    bl = config.get("member_blocklist") or []
    if isinstance(bl, str):
        bl = [x.strip() for x in bl.split(",") if x.strip()]
    elif isinstance(bl, (list, tuple)):
        bl = [str(x).strip() for x in bl if str(x).strip()]
    else:
        bl = []
    out["member_blocklist"] = bl
    for rt, dflt in DEFAULT_CONFIG.items():
        user = config.get(rt)
        if not isinstance(user, dict):
            continue
        is_group = rt in ("1", "3")
        modes = GROUP_MODES if is_group else PRIVATE_MODES
        mode = user.get("mode")
        if mode in modes:
            out[rt]["mode"] = mode
        try:
            w = int(user.get("merge_window", dflt["merge_window"]))
            if w >= 1:
                out[rt]["merge_window"] = w
        except (TypeError, ValueError):
            pass
        if is_group:
            try:
                t = int(user.get("track_duration", dflt.get("track_duration", 180)))
                if t >= 0:
                    out[rt]["track_duration"] = t
            except (TypeError, ValueError):
                pass
    return out


def _config_from_legacy(reply_all: int, cooldown_seconds: int) -> dict:
    """旧配置迁移：reply_all(-1/0/1) + cooldown_seconds → 新四类 config。
    注意：冷却(回复后间隔) 与 合并窗口(入队后聚合) 语义不同，此处仅作初值，
    管理员应复核窗口大小。"""
    w = max(1, int(cooldown_seconds or 30))
    if reply_all == -1:
        modes = {"1": "silent", "2": "silent", "3": "silent", "4": "silent"}
    elif reply_all == 0:
        modes = {"1": "at_only", "2": "reply", "3": "at_only", "4": "reply"}
    else:
        modes = {"1": "reply_all", "2": "reply", "3": "reply_all", "4": "reply"}
    cfg = {}
    for rt, dflt in DEFAULT_CONFIG.items():
        entry = {"mode": modes[rt], "merge_window": w}
        if rt in ("1", "3"):
            entry["track_duration"] = dflt["track_duration"]
        cfg[rt] = entry
    return cfg


def _mode_summary(config: dict) -> int:
    """按四类模式汇总出兼容的 reply_all 摘要：-1=全部不回复 0=仅@ 1=有回复"""
    modes = [
        c.get("mode")
        for c in config.values()
        if isinstance(c, dict) and c.get("mode")
    ]
    if modes and all(m == "silent" for m in modes):
        return -1
    if any(m in ("reply", "reply_all") for m in modes):
        return 1
    return 0


class RobotService:
    """Service for robot registration and management using MySQL"""

    def __init__(self):
        self._pool: Optional[aiomysql.Pool] = None

    def set_pool(self, pool: aiomysql.Pool):
        """Set the database connection pool"""
        self._pool = pool

    async def ensure_table(self):
        """Create robots table if not exists"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS robots (
                        robot_id     VARCHAR(10)  PRIMARY KEY,
                        token        VARCHAR(64)  NOT NULL,
                        device_name  VARCHAR(100) NOT NULL DEFAULT '',
                        remark       VARCHAR(200) NOT NULL DEFAULT '',
                        status       VARCHAR(20)  NOT NULL DEFAULT 'offline',
                        last_seen    DATETIME     DEFAULT NULL,
                        created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        ws_url       VARCHAR(200) NOT NULL DEFAULT '',
                        reply_all    INT          NOT NULL DEFAULT 0
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                # 兼容已存在的旧表：幂等补列
                try:
                    await cur.execute(
                        "ALTER TABLE robots ADD COLUMN reply_all INT NOT NULL DEFAULT 0"
                    )
                except Exception:
                    pass  # 列已存在
                try:
                    await cur.execute(
                        "ALTER TABLE robots ADD COLUMN reply_strategy_id INT DEFAULT NULL"
                    )
                except Exception:
                    pass  # 列已存在
                try:
                    await cur.execute(
                        "ALTER TABLE robots ADD COLUMN agent_id INT DEFAULT NULL"
                    )
                except Exception:
                    pass  # 列已存在
                try:
                    await cur.execute(
                        "ALTER TABLE robots ADD COLUMN auto_reply TINYINT NOT NULL DEFAULT 1"
                    )
                except Exception:
                    pass  # 列已存在
                # 回复策略组表（v2：四类聊天室配置 JSON + 设备级限额；
                # reply_all/cooldown_seconds 为旧列，仅迁移读取用）
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS reply_strategies (
                        id                   INT AUTO_INCREMENT PRIMARY KEY,
                        name                 VARCHAR(100) NOT NULL,
                        config               TEXT,
                        max_concurrent_llm   INT          NOT NULL DEFAULT 2,
                        max_calls_per_min    INT          NOT NULL DEFAULT 5,
                        is_default           TINYINT      NOT NULL DEFAULT 0,
                        created_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        reply_all            INT          NOT NULL DEFAULT 1,
                        cooldown_seconds     INT          NOT NULL DEFAULT 30
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                # 兼容已存在的旧表：幂等补列
                for col in (
                    "config TEXT",
                    "max_concurrent_llm INT NOT NULL DEFAULT 2",
                    "max_calls_per_min INT NOT NULL DEFAULT 5",
                ):
                    try:
                        await cur.execute(f"ALTER TABLE reply_strategies ADD COLUMN {col}")
                    except Exception:
                        pass  # 列已存在
                await conn.commit()
        # 种子默认策略 + 迁移现有设备
        await self._seed_strategies()
        logger.info("✅ Robots & reply_strategies tables ensured")

    async def _seed_strategies(self):
        """创建默认策略（不可修改/删除），迁移旧策略 config 列，并按 reply_all 迁移老设备策略归属"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                # 1. 默认策略（v2 结构：默认配置 + 限额）
                await cur.execute(
                    "SELECT id FROM reply_strategies WHERE is_default=1 LIMIT 1"
                )
                dflt = await cur.fetchone()
                if dflt is None:
                    await cur.execute(
                        """INSERT INTO reply_strategies
                           (name, config, max_concurrent_llm, max_calls_per_min, is_default)
                           VALUES (%s,%s,%s,%s,1)""",
                        (
                            "默认策略",
                            json.dumps(DEFAULT_CONFIG, ensure_ascii=False),
                            DEFAULT_MAX_CONCURRENT_LLM,
                            DEFAULT_MAX_CALLS_PER_MIN,
                        ),
                    )
                    dflt_id = cur.lastrowid
                else:
                    dflt_id = dflt[0]

                # 2. 迁移：config 为空的老策略按 reply_all/cooldown_seconds 生成新配置
                await cur.execute(
                    "SELECT id, reply_all, cooldown_seconds, config FROM reply_strategies"
                )
                legacy_rows = await cur.fetchall()
                migrated = 0
                for sid, reply_all, cooldown, raw_cfg in legacy_rows:
                    if raw_cfg:
                        continue
                    cfg = _config_from_legacy(
                        reply_all if reply_all is not None else 1,
                        cooldown if cooldown is not None else 30,
                    )
                    await cur.execute(
                        "UPDATE reply_strategies SET config=%s WHERE id=%s",
                        (json.dumps(cfg, ensure_ascii=False), sid),
                    )
                    migrated += 1
                if migrated:
                    logger.warning(
                        f"♻️ 迁移 {migrated} 个策略组配置（旧 reply_all+冷却 → 四类 config；"
                        f"冷却时间已作为合并窗口初值，请在管理页复核）"
                    )

                # 3. 迁移：reply_strategy_id 为空的老设备，按其 reply_all 归入对应策略
                await cur.execute(
                    "SELECT robot_id, reply_all FROM robots WHERE reply_strategy_id IS NULL"
                )
                rows = await cur.fetchall()
                for robot_id, reply_all in rows:
                    reply_all = reply_all if reply_all is not None else 1
                    if reply_all == 1:
                        sid = dflt_id
                    else:
                        sid = await self._find_or_create_strategy(cur, reply_all)
                    await cur.execute(
                        "UPDATE robots SET reply_strategy_id=%s WHERE robot_id=%s",
                        (sid, robot_id),
                    )
                await conn.commit()
                if rows:
                    logger.info(
                        f"Migrated {len(rows)} robot(s) to reply strategy groups"
                    )

    async def _find_or_create_strategy(self, cur, reply_all: int) -> int:
        """查找与 reply_all 相同的非默认策略，不存在则创建（仅迁移用）"""
        await cur.execute(
            "SELECT id FROM reply_strategies WHERE is_default=0 AND reply_all=%s LIMIT 1",
            (reply_all,),
        )
        row = await cur.fetchone()
        if row:
            return row[0]
        names = {-1: "只读不回调", 0: "仅@回复", 1: "全部回调"}
        cfg = _config_from_legacy(reply_all, 4)
        await cur.execute(
            """INSERT INTO reply_strategies
               (name, config, max_concurrent_llm, max_calls_per_min, is_default, reply_all, cooldown_seconds)
               VALUES (%s,%s,%s,%s,0,%s,4)""",
            (
                names.get(reply_all, f"策略-{reply_all}"),
                json.dumps(cfg, ensure_ascii=False),
                DEFAULT_MAX_CONCURRENT_LLM,
                DEFAULT_MAX_CALLS_PER_MIN,
                reply_all,
            ),
        )
        return cur.lastrowid

    # ── helper ────────────────────────────────────────────

    def _row_to_robot(self, row) -> RobotInfo:
        """Convert a DB row to RobotInfo"""
        return RobotInfo(
            robot_id=row[0],
            token=row[1],
            device_name=row[2] or "",
            remark=row[3] or "",
            status=row[4],
            last_seen=row[5].isoformat() if row[5] else None,
            created_at=row[6].isoformat() if row[6] else "",
            ws_url=row[7] or "",
            reply_all=row[8] if row[8] is not None else 0,
            reply_strategy_id=row[9] if len(row) > 9 else None,
            strategy_name=(row[10] or "") if len(row) > 10 else "",
            agent_id=row[11] if len(row) > 11 else None,
            agent_name=(row[12] or "") if len(row) > 12 else "",
            auto_reply=(row[13] == 1) if len(row) > 13 else True,
        )

    # ── core operations ───────────────────────────────────

    async def register(
        self, device_name: str = "", remark: str = ""
    ) -> Tuple[str, str]:
        """
        Register a new robot

        Returns:
            Tuple of (robot_id, token)
        """
        # Generate unique 6-digit numeric robot ID
        while True:
            robot_id = f"{random.randint(0, 999999):06d}"
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT 1 FROM robots WHERE robot_id=%s", (robot_id,)
                    )
                    if not await cur.fetchone():
                        break

        # Generate token
        raw = f"{robot_id}:{uuid.uuid4().hex}"
        token = hashlib.sha256(raw.encode()).hexdigest()[:32]

        now = datetime.now()
        ws_url = f"ws://{{host}}:{{port}}/webserver/wework/{robot_id}"

        # Insert into DB（新设备默认使用默认回复策略组）
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM reply_strategies WHERE is_default=1 LIMIT 1"
                )
                dflt = await cur.fetchone()
                default_strategy_id = dflt[0] if dflt else None
                await cur.execute(
                    """INSERT INTO robots
                       (robot_id, token, device_name, remark, status, created_at, ws_url, reply_strategy_id)
                       VALUES (%s,%s,%s,%s,'offline',%s,%s,%s)""",
                    (
                        robot_id,
                        token,
                        device_name or f"Device_{robot_id[-4:]}",
                        remark,
                        now,
                        ws_url,
                        default_strategy_id,
                    ),
                )
                await conn.commit()

        logger.info(f"Registered new robot: {robot_id}")
        return robot_id, token

    async def get_robot(self, robot_id: str) -> Optional[RobotInfo]:
        """Get robot info by ID"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT r.robot_id,r.token,r.device_name,r.remark,r.status,r.last_seen,
                              r.created_at,r.ws_url,r.reply_all,r.reply_strategy_id,s.name,
                              r.agent_id,a.name,r.auto_reply
                       FROM robots r
                       LEFT JOIN reply_strategies s ON r.reply_strategy_id = s.id
                       LEFT JOIN agents a ON r.agent_id = a.id
                       WHERE r.robot_id=%s""",
                    (robot_id,),
                )
                row = await cur.fetchone()
        if row:
            return self._row_to_robot(row)
        return None

    async def validate_robot(self, robot_id: str) -> Tuple[bool, str]:
        """
        Validate if a robot can connect.
        Unknown IDs get auto-registered (lazy registration).
        """
        robot = await self.get_robot(robot_id)

        if robot is None:
            # Auto-register
            await self.register(device_name=f"Auto_{robot_id[-4:]}")
            logger.info(f"Auto-registered robot: {robot_id}")
            return True, ""

        if robot.status == RobotStatus.BANNED:
            return False, f"Robot {robot_id} has been banned"

        return True, ""

    async def update_status(self, robot_id: str, status: RobotStatus):
        """Update robot status and last_seen"""
        now = datetime.now()
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                if status == RobotStatus.ONLINE:
                    await cur.execute(
                        "UPDATE robots SET status=%s, last_seen=%s WHERE robot_id=%s",
                        (status, now, robot_id),
                    )
                else:
                    await cur.execute(
                        "UPDATE robots SET status=%s WHERE robot_id=%s",
                        (status, robot_id),
                    )
                await conn.commit()

    async def set_online(self, robot_id: str):
        """Mark robot as online"""
        await self.update_status(robot_id, RobotStatus.ONLINE)

    async def set_offline(self, robot_id: str):
        """Mark robot as offline"""
        await self.update_status(robot_id, RobotStatus.OFFLINE)

    async def reset_all_online(self):
        """服务启动时重置所有在线设备为离线（服务端重启后旧连接全部失效，
        避免设备管理页显示脏'在线'状态；真实状态以 active_connections 为准）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE robots SET status='offline' WHERE status='online'"
                )
                await conn.commit()
                affected = cur.rowcount
        if affected:
            logger.info(f"♻️ Reset {affected} robot(s) to offline on startup")

    async def ban_robot(self, robot_id: str) -> bool:
        """Ban a robot"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE robots SET status='banned' WHERE robot_id=%s", (robot_id,)
                )
                await conn.commit()
                affected = cur.rowcount
        if affected:
            logger.info(f"Banned robot: {robot_id}")
            return True
        return False

    async def unban_robot(self, robot_id: str) -> bool:
        """Unban a robot"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE robots SET status='offline' WHERE robot_id=%s", (robot_id,)
                )
                await conn.commit()
                affected = cur.rowcount
        if affected:
            logger.info(f"Unbanned robot: {robot_id}")
            return True
        return False

    async def delete_robot(self, robot_id: str) -> bool:
        """Delete a robot registration (also cleans up group memberships)"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM robots WHERE robot_id=%s", (robot_id,))
                await conn.commit()
                affected = cur.rowcount
        if affected:
            # 清理分组关系（robot_groups / org_group_members 由 group_service 建表，共用一个连接池）
            try:
                async with self._pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "DELETE FROM robot_groups WHERE robot_id=%s", (robot_id,)
                        )
                        await cur.execute(
                            "DELETE FROM org_group_members "
                            "WHERE member_type='device' AND member_id=%s",
                            (robot_id,),
                        )
                        # 设备级长期记忆绑定随设备一起删（记忆集本身与事实不动）
                        await cur.execute(
                            "DELETE FROM robot_memory_sets WHERE robot_id=%s", (robot_id,)
                        )
                        await conn.commit()
            except Exception as e:
                logger.warning(f"clean group memberships for {robot_id} failed: {e}")
            logger.info(f"Deleted robot: {robot_id}")
            return True
        return False

    async def update_reply_all(self, robot_id: str, reply_all: int) -> bool:
        """Update robot reply strategy (-1=只读不回调, 0=仅@回复, 1=全部回调)"""
        if reply_all not in (-1, 0, 1):
            raise ValueError("reply_all must be -1, 0, or 1")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM robots WHERE robot_id=%s", (robot_id,))
                if not await cur.fetchone():
                    logger.warning(f"⚠️ Robot not found: {robot_id}")
                    return False
                await cur.execute(
                    "UPDATE robots SET reply_all=%s WHERE robot_id=%s",
                    (int(reply_all), robot_id),
                )
                await conn.commit()
        # rowcount 在值未变化时为 0，不能据此判定失败；设备存在且 UPDATE 成功即视为成功
        logger.info(f"✅ Updated reply_all of {robot_id} to {reply_all}")
        return True

    async def update_auto_reply(self, robot_id: str, enabled: bool) -> bool:
        """Update robot auto-reply master switch.
        语义（v2）：True=开启「推送 LLM」——消息照常接收，队列按策略推送智能体；
        False=关闭=设备级静默——消息照常接收存储，但不入队、不推送、不回复。"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM robots WHERE robot_id=%s", (robot_id,))
                if not await cur.fetchone():
                    logger.warning(f"⚠️ Robot not found: {robot_id}")
                    return False
                await cur.execute(
                    "UPDATE robots SET auto_reply=%s WHERE robot_id=%s",
                    (1 if enabled else 0, robot_id),
                )
                await conn.commit()
        # rowcount 在值未变化时为 0，不能据此判定失败；设备存在且 UPDATE 成功即视为成功
        logger.info(f"✅ Updated auto_reply of {robot_id} to {enabled}")
        return True

    async def list_robots(self) -> List[RobotInfo]:
        """List all registered robots"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT r.robot_id,r.token,r.device_name,r.remark,r.status,r.last_seen,
                              r.created_at,r.ws_url,r.reply_all,r.reply_strategy_id,s.name,
                              r.agent_id,a.name,r.auto_reply
                       FROM robots r
                       LEFT JOIN reply_strategies s ON r.reply_strategy_id = s.id
                       LEFT JOIN agents a ON r.agent_id = a.id
                       ORDER BY r.created_at DESC"""
                )
                rows = await cur.fetchall()
        return [self._row_to_robot(r) for r in rows]

    # ── reply strategy groups ─────────────────────────────

    async def list_strategies(self) -> List[ReplyStrategy]:
        """List all reply strategy groups (default first)"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT s.id, s.name, s.config, s.max_concurrent_llm, s.max_calls_per_min,
                              s.is_default, s.created_at,
                              (SELECT COUNT(*) FROM robots r WHERE r.reply_strategy_id = s.id) AS robot_count
                       FROM reply_strategies s
                       ORDER BY s.is_default DESC, s.id ASC"""
                )
                rows = await cur.fetchall()
        return [
            ReplyStrategy(
                id=r[0],
                name=r[1],
                config=self._parse_config(r[2]),
                max_concurrent_llm=r[3] if r[3] is not None else DEFAULT_MAX_CONCURRENT_LLM,
                max_calls_per_min=r[4] if r[4] is not None else DEFAULT_MAX_CALLS_PER_MIN,
                is_default=bool(r[5]),
                created_at=r[6].isoformat() if r[6] else "",
                robot_count=r[7],
            )
            for r in rows
        ]

    async def get_strategy(self, strategy_id: int) -> Optional[ReplyStrategy]:
        """Get a strategy group by id"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT id,name,config,max_concurrent_llm,max_calls_per_min,is_default,created_at
                       FROM reply_strategies WHERE id=%s""",
                    (strategy_id,),
                )
                row = await cur.fetchone()
        if row is None:
            return None
        return self._row_to_strategy(row)

    async def get_robot_strategy(self, robot_id: str) -> Optional[ReplyStrategy]:
        """Get the strategy group bound to a robot"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT s.id, s.name, s.config, s.max_concurrent_llm, s.max_calls_per_min,
                              s.is_default, s.created_at
                       FROM robots r JOIN reply_strategies s ON r.reply_strategy_id = s.id
                       WHERE r.robot_id=%s""",
                    (robot_id,),
                )
                row = await cur.fetchone()
        if row is None:
            return None
        return self._row_to_strategy(row)

    @staticmethod
    def _parse_config(raw) -> dict:
        """解析 config 列，失败/缺失回退默认配置"""
        if isinstance(raw, dict):
            return normalize_config(raw)
        if isinstance(raw, str) and raw.strip():
            try:
                return normalize_config(json.loads(raw))
            except Exception:
                pass
        dflt = json.loads(json.dumps(DEFAULT_CONFIG))
        dflt["member_blocklist"] = []
        return dflt

    def _row_to_strategy(self, row) -> ReplyStrategy:
        cfg = self._parse_config(row[2])
        return ReplyStrategy(
            id=row[0],
            name=row[1],
            config=cfg,
            max_concurrent_llm=row[3] if row[3] is not None else DEFAULT_MAX_CONCURRENT_LLM,
            max_calls_per_min=row[4] if row[4] is not None else DEFAULT_MAX_CALLS_PER_MIN,
            is_default=bool(row[5]),
            created_at=row[6].isoformat() if len(row) > 6 and row[6] else "",
            reply_all=_mode_summary(cfg),
        )

    async def create_strategy(
        self,
        name: str,
        config: dict,
        max_concurrent_llm: int = DEFAULT_MAX_CONCURRENT_LLM,
        max_calls_per_min: int = DEFAULT_MAX_CALLS_PER_MIN,
    ) -> int:
        """Create a new reply strategy group. Returns new id."""
        name = name.strip()
        if not name:
            raise ValueError("策略名称不能为空")
        cfg = normalize_config(config)
        max_concurrent_llm = max(1, int(max_concurrent_llm or DEFAULT_MAX_CONCURRENT_LLM))
        max_calls_per_min = max(1, int(max_calls_per_min or DEFAULT_MAX_CALLS_PER_MIN))
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM reply_strategies WHERE name=%s", (name,)
                )
                if await cur.fetchone():
                    raise ValueError(f"策略名称「{name}」已存在")
                await cur.execute(
                    """INSERT INTO reply_strategies
                       (name, config, max_concurrent_llm, max_calls_per_min, is_default)
                       VALUES (%s,%s,%s,%s,0)""",
                    (name, json.dumps(cfg, ensure_ascii=False), max_concurrent_llm, max_calls_per_min),
                )
                new_id = cur.lastrowid
                await conn.commit()
        logger.info(
            f"✅ Created reply strategy: {name} (limits: {max_concurrent_llm}并发/{max_calls_per_min}每分钟)"
        )
        return new_id

    async def update_strategy(
        self,
        strategy_id: int,
        name: str,
        config: dict,
        max_concurrent_llm: int = DEFAULT_MAX_CONCURRENT_LLM,
        max_calls_per_min: int = DEFAULT_MAX_CALLS_PER_MIN,
    ) -> bool:
        """Update a strategy group. Default strategy is not editable.
        Changes take effect on all bound robots automatically (reference)."""
        name = name.strip()
        if not name:
            raise ValueError("策略名称不能为空")
        cfg = normalize_config(config)
        max_concurrent_llm = max(1, int(max_concurrent_llm or DEFAULT_MAX_CONCURRENT_LLM))
        max_calls_per_min = max(1, int(max_calls_per_min or DEFAULT_MAX_CALLS_PER_MIN))
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT is_default FROM reply_strategies WHERE id=%s",
                    (strategy_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return False
                if row[0]:
                    raise ValueError("默认策略不可修改")
                await cur.execute(
                    "SELECT id FROM reply_strategies WHERE name=%s AND id<>%s",
                    (name, strategy_id),
                )
                if await cur.fetchone():
                    raise ValueError(f"策略名称「{name}」已被其他策略使用")
                await cur.execute(
                    """UPDATE reply_strategies SET name=%s, config=%s,
                       max_concurrent_llm=%s, max_calls_per_min=%s WHERE id=%s""",
                    (name, json.dumps(cfg, ensure_ascii=False), max_concurrent_llm, max_calls_per_min, strategy_id),
                )
                await conn.commit()
        # rowcount 在值未变化时为 0，不能据此判定失败；策略存在性已在上方检查
        logger.info(
            f"✅ Updated reply strategy #{strategy_id}: {name} (limits: {max_concurrent_llm}并发/{max_calls_per_min}每分钟)"
        )
        return True

    async def delete_strategy(self, strategy_id: int) -> Tuple[bool, int]:
        """Delete a strategy group. Default strategy cannot be deleted.
        Robots bound to it are rolled back to the default strategy.
        Returns (success, affected_robot_count)."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT is_default FROM reply_strategies WHERE id=%s",
                    (strategy_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return False, 0
                if row[0]:
                    raise ValueError("默认策略不可删除")
                await cur.execute(
                    "SELECT id FROM reply_strategies WHERE is_default=1 LIMIT 1"
                )
                dflt = await cur.fetchone()
                default_id = dflt[0] if dflt else None
                await cur.execute(
                    "UPDATE robots SET reply_strategy_id=%s WHERE reply_strategy_id=%s",
                    (default_id, strategy_id),
                )
                affected = cur.rowcount
                await cur.execute(
                    "DELETE FROM reply_strategies WHERE id=%s", (strategy_id,)
                )
                await conn.commit()
        logger.info(
            f"🗑 Deleted reply strategy #{strategy_id}, {affected} robot(s) rolled back to default"
        )
        return True, affected

    async def update_robot_strategy(self, robot_id: str, strategy_id: int) -> bool:
        """Bind a strategy group to a robot"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM reply_strategies WHERE id=%s", (strategy_id,)
                )
                if not await cur.fetchone():
                    return False
                await cur.execute(
                    "UPDATE robots SET reply_strategy_id=%s WHERE robot_id=%s",
                    (strategy_id, robot_id),
                )
                await conn.commit()
                affected = cur.rowcount
        if affected:
            logger.info(f"✅ Bound robot {robot_id} to reply strategy #{strategy_id}")
            return True
        return False

    async def get_stats(self) -> dict:
        """Get registration statistics"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT status, COUNT(*) FROM robots GROUP BY status")
                rows = await cur.fetchall()

        counts = {"online": 0, "offline": 0, "banned": 0}
        for status, cnt in rows:
            counts[status] = cnt

        return {
            "total": sum(counts.values()),
            **counts,
        }


# Global service instance (pool will be set during startup)
robot_service = RobotService()
