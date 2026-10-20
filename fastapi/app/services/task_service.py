"""
任务模块服务（TaskService）

任务 = 触发器 × 动作
- 触发器 trigger_type:
    schedule   定时触发：cron5 表达式（0 9 * * *）或简单间隔（每 N 秒）
    condition  条件触发：friend_accepted（好友申请通过）/ message_from（收到某人消息）
- 动作 action_type:
    workflow  执行指定工作流（workflow_engine.execute）
    agent     向指定智能体注入指令（agent_service.generate_reply，可回复到目标会话）

──────────────────────────────────────────────────────
核心设计：条件检测三层策略（回应"如何高效识别好友申请通过，且不影响其他工作"）

  1. 事件流（零额外成本，首选）
     设备端本来就实时上报：101（收到消息）、502（好友资料/自动通过好友上报）。
     条件引擎订阅 aivbot_service 事件，收到匹配事件即触发——完全不需要轮询。

  2. 轻量信号（近零成本）
     message_from 直接由 101 消息流匹配（单聊 room == target）。
     friend_accepted 收到目标用户消息时唤醒轮询器做一次复核（快，不忙轮询）。

  3. 共享增量轮询（兜底，成本可控）
     只有事件覆盖不到的条件才轮询，且**每台设备只有一个共享轮询器**（不是每任务一个）：
       - 低频后台任务拉一次 507 全量好友 → 与上次结果比对 → 命中目标即触发；
       - N 个任务等同一个好友，只拉一次；
       - 自适应频率（有待处理任务 60s，空闲 600s）+ 随机抖动（防整点惊群）；
       - 设备离线/忙时跳过本轮（不影响自动回复等实时工作）；
       - 独立 asyncio 后台任务，与主消息循环完全隔离。

  触发链：信号 → 原子占位（幂等，防事件+轮询双重触发）→ 执行动作 → 记录 task_runs。
──────────────────────────────────────────────────────
"""
import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .aivbot_service import aivbot_service
from .agent_service import agent_service
from .workflow_engine import workflow_engine
from ..models.task import STATE_DONE, STATE_IDLE, STATE_WAITING

logger = logging.getLogger(__name__)

# tasks 表列（顺序与 row_to_task 下标对应）
_COL_LIST = "id,name,org_id,robot_id,trigger_type,trigger_spec,action_type,action_spec,enabled,state,next_run_at,expires_at,run_count,last_run_at,last_result,created_at,updated_at"

# task_runs 表列
_RUN_COLS = "id,task_id,trigger_desc,status,result,started_at,finished_at"

# 轮询参数
POLL_INTERVAL_PENDING = 60    # 有待处理条件任务时的轮询间隔（秒）
POLL_INTERVAL_IDLE = 600      # 空闲间隔（秒）
POLL_CONTACTS_TIMEOUT = 10    # 等待 507 回执写入的超时（秒）
SCHEDULER_SLEEP_IDLE = 5.0    # 调度循环无任务时的休眠（秒）
SCHEDULER_SLEEP_CAP = 300.0   # 调度循环最长休眠（秒）


# ──────────────────────────────────────────────────
# cron 5 段表达式工具（不引入第三方依赖）
# ──────────────────────────────────────────────────
def _parse_cron_field(field: str, lo: int, hi: int) -> set:
    """解析 cron 字段：支持 *、*/n、a-b、a-b/n、a,b,c（不支持 L/W/# 特殊字符）"""
    out: set = set()
    if field == "*":
        return set(range(lo, hi + 1))
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            step = int(step_s)
        if part in ("", "*"):
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            start, end = int(a), int(b)
        else:
            out.add(int(part))
            continue
        for v in range(start, end + 1, step):
            out.add(v)
    return out


def next_cron_time(expr: str, after_ts: float) -> Optional[int]:
    """计算 5 段 cron 表达式在 after_ts 之后的下一次执行时间戳（秒），无效返回 None。

    cron 中 日/周 均为受限时按标准 OR 语义（任一命中即可）。
    """
    fields = expr.split()
    if len(fields) != 5:
        return None
    try:
        minutes = _parse_cron_field(fields[0], 0, 59)
        hours = _parse_cron_field(fields[1], 0, 23)
        doms = _parse_cron_field(fields[2], 1, 31)
        months = _parse_cron_field(fields[3], 1, 12)
        dows = _parse_cron_field(fields[4], 0, 6)  # cron: 0=周日
    except Exception:
        return None
    dom_restricted = fields[2] != "*"
    dow_restricted = fields[4] != "*"
    dt = datetime.fromtimestamp(after_ts) + timedelta(minutes=1)
    dt = dt.replace(second=0, microsecond=0)
    limit = dt + timedelta(days=366)
    while dt < limit:
        if dt.month in months and dt.hour in hours and dt.minute in minutes:
            dom_ok = dt.day in doms
            dow_ok = (dt.weekday() + 1) % 7 in dows  # cron 0=周日
            if not dom_restricted and not dow_restricted:
                return int(dt.timestamp())
            if dom_restricted and dow_restricted:
                if dom_ok or dow_ok:
                    return int(dt.timestamp())
            elif dom_ok if dom_restricted else dow_ok:
                return int(dt.timestamp())
        dt += timedelta(minutes=1)
    return None


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"


# ── 时间选择 UI ↔ cron5 ──────────────────────────────
# 表单用「执行时间 + 重复频率（每天/每周/每月）」替代手写 cron，
# 生成/解析标准 cron5：分 时 日 月 周（0=周日）。
_WEEKDAY_LABELS = ["日", "一", "二", "三", "四", "五", "六"]


def build_cron_from_form(time_str, freq="daily", weekdays=None, monthday=None) -> Optional[str]:
    """由时间选择组件生成 cron5 表达式；参数非法返回 None"""
    if not time_str:
        return None
    t = str(time_str).strip()
    if ":" not in t:
        return None
    try:
        hour, minute = int(t.split(":")[0]), int(t.split(":")[1])
    except (ValueError, TypeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    if freq == "weekly":
        days = sorted({int(d) for d in (weekdays or []) if str(d) in "0123456"})
        if not days:
            return None
        return f"{minute} {hour} * * {','.join(str(d) for d in days)}"
    if freq == "monthly":
        try:
            dom = int(monthday)
        except (TypeError, ValueError):
            return None
        if not 1 <= dom <= 31:
            return None
        return f"{minute} {hour} {dom} * *"
    return f"{minute} {hour} * * *"


def cron_to_form(cron) -> Optional[dict]:
    """把 cron5 反解析为时间组件值 {time, freq, weekdays, monthday}；
    非「每天/每周/每月」简单形式（如 */5、日月同时限定）返回 None"""
    if not cron:
        return None
    fields = str(cron).split()
    if len(fields) != 5:
        return None
    try:
        minute, hour = int(fields[0]), int(fields[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    hm = f"{hour:02d}:{minute:02d}"
    dom, dow = fields[2], fields[4]
    if dom == "*" and dow == "*":
        return {"time": hm, "freq": "daily", "weekdays": [], "monthday": 1}
    if dom == "*":
        try:
            days = sorted({int(d) for d in dow.split(",") if d.strip() in "0123456"})
        except ValueError:
            return None
        if not days:
            return None
        return {"time": hm, "freq": "weekly", "weekdays": days, "monthday": 1}
    if dow == "*":
        try:
            dom_v = int(dom)
        except (TypeError, ValueError):
            return None
        if not 1 <= dom_v <= 31:
            return None
        return {"time": hm, "freq": "monthly", "weekdays": [], "monthday": dom_v}
    return None


def parse_datetime_local(s) -> Optional[int]:
    """解析 <input type="datetime-local"> 值（YYYY-MM-DDTHH:MM）或纯数字时间戳为秒级时间戳"""
    if s is None or str(s).strip() == "":
        return None
    t = str(s).strip()
    if t.isdigit():
        return int(t)
    try:
        return int(datetime.strptime(t, "%Y-%m-%dT%H:%M").timestamp())
    except (ValueError, TypeError):
        return None


# ──────────────────────────────────────────────────
# 任务服务
# ──────────────────────────────────────────────────
class TaskService:
    def __init__(self):
        self._pool = None
        self._running = False
        self._scheduler_task: Optional[asyncio.Task] = None
        self._event_task: Optional[asyncio.Task] = None
        self._poll_tasks: Dict[str, asyncio.Task] = {}          # robot_id -> 轮询器
        self._poll_wake: Dict[str, asyncio.Event] = {}          # robot_id -> 唤醒事件
        self._event_queue: asyncio.Queue = asyncio.Queue()      # 设备事件队列
        self._exec_tasks: set = set()                           # 后台执行任务

    # ── 生命周期 ──────────────────────────────────

    def set_pool(self, pool):
        self._pool = pool

    async def ensure_table(self):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS tasks (
                        id           INT AUTO_INCREMENT PRIMARY KEY,
                        name         VARCHAR(100) NOT NULL,
                        org_id       INT NOT NULL DEFAULT 0,
                        robot_id     VARCHAR(10) NOT NULL DEFAULT '',
                        trigger_type VARCHAR(20) NOT NULL,
                        trigger_spec TEXT,
                        action_type  VARCHAR(20) NOT NULL,
                        action_spec  TEXT,
                        enabled      TINYINT NOT NULL DEFAULT 1,
                        state        VARCHAR(20) NOT NULL DEFAULT 'idle',
                        next_run_at  BIGINT NOT NULL DEFAULT 0,
                        expires_at   BIGINT NOT NULL DEFAULT 0,
                        run_count    INT NOT NULL DEFAULT 0,
                        last_run_at  BIGINT NOT NULL DEFAULT 0,
                        last_result  VARCHAR(20) NOT NULL DEFAULT '',
                        created_at   BIGINT NOT NULL,
                        updated_at   BIGINT NOT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                )
                await cur.execute(
                    """CREATE TABLE IF NOT EXISTS task_runs (
                        id           INT AUTO_INCREMENT PRIMARY KEY,
                        task_id      INT NOT NULL,
                        trigger_desc VARCHAR(200) NOT NULL DEFAULT '',
                        status       VARCHAR(20) NOT NULL DEFAULT 'running',
                        result       TEXT,
                        started_at   BIGINT NOT NULL,
                        finished_at  BIGINT NOT NULL DEFAULT 0
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
                )
                try:
                    await cur.execute(
                        "CREATE INDEX idx_tasks_next_run ON tasks (next_run_at)"
                    )
                except Exception:
                    pass  # 索引已存在
        logger.info("✅ 任务模块表就绪（tasks / task_runs）")

    async def start(self):
        """启动调度循环 / 事件消费 / 条件轮询（main.py startup 时调用）"""
        if self._running or self._pool is None:
            return
        self._running = True
        # 重启恢复：把卡在 running 的任务复位
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """UPDATE tasks SET state = CASE WHEN trigger_type='condition'
                           THEN 'waiting' ELSE 'idle' END
                           WHERE state='running'"""
                    )
        except Exception:
            logger.exception("任务状态复位失败")
        # 注册设备事件监听（aivbot_service 事件 → 队列）
        try:
            aivbot_service.add_event_listener(self._on_device_event)
        except Exception:
            logger.exception("注册设备事件监听失败")
        loop = asyncio.get_event_loop()
        self._scheduler_task = loop.create_task(self._scheduler_loop())
        self._event_task = loop.create_task(self._event_consumer())
        logger.info("🚀 任务调度器已启动（定时 + 条件触发）")

    async def stop(self):
        self._running = False
        for t in list(self._poll_tasks.values()):
            t.cancel()
        self._poll_tasks.clear()
        for t in (self._scheduler_task, self._event_task):
            if t:
                t.cancel()

    # ── 行 → dict ──────────────────────────────────

    @staticmethod
    def _row_to_task(r) -> dict:
        return {
            "id": r[0], "name": r[1], "org_id": r[2], "robot_id": r[3],
            "trigger_type": r[4], "trigger_spec": r[5], "action_type": r[6],
            "action_spec": r[7], "enabled": bool(r[8]), "state": r[9],
            "next_run_at": r[10] or 0, "expires_at": r[11] or 0,
            "run_count": r[12] or 0, "last_run_at": r[13] or 0,
            "last_result": r[14] or "", "created_at": r[15] or 0,
            "updated_at": r[16] or 0,
        }

    @staticmethod
    def _row_to_run(r) -> dict:
        return {
            "id": r[0], "task_id": r[1], "trigger_desc": r[2] or "",
            "status": r[3], "result": r[4], "started_at": r[5] or 0,
            "finished_at": r[6] or 0,
        }

    @staticmethod
    def _row_to_robot_id(r) -> str:
        return r[0] if r else ""

    # ── CRUD ──────────────────────────────────────

    async def create_task(self, req) -> Tuple[bool, Any]:
        """创建任务，返回 (ok, id 或错误信息)"""
        trigger_type = req.trigger_type
        if trigger_type not in ("schedule", "condition"):
            return False, "trigger_type 必须是 schedule 或 condition"
        spec = dict(req.trigger_spec or {})
        if trigger_type == "schedule":
            if not (spec.get("cron") or spec.get("interval_sec") or spec.get("once_at") is not None):
                return False, "定时任务需要 cron 表达式、间隔秒数或单次执行时间 once_at"
            if spec.get("cron") and next_cron_time(str(spec["cron"]), time.time()) is None:
                return False, f"cron 表达式无效: {spec.get('cron')}"
            if spec.get("once_at") is not None:
                try:
                    once = int(spec["once_at"])
                except (TypeError, ValueError):
                    return False, "once_at 必须是秒级时间戳"
                if once <= int(time.time()):
                    return False, "单次执行时间 once_at 必须晚于当前时间"
        else:
            ctype = spec.get("type")
            if ctype not in ("friend_accepted", "message_from"):
                return False, "条件类型必须是 friend_accepted 或 message_from"
            if not req.robot_id:
                return False, "条件任务必须指定 robot_id（设备）"
            if ctype == "friend_accepted":
                if not (spec.get("target") or {}):
                    return False, "friend_accepted 条件需要 target（如 {\"phone\": \"138...\"}）"
            elif not spec.get("target"):
                return False, "message_from 条件需要 target（对方昵称）"
        if req.action_type not in ("workflow", "agent"):
            return False, "action_type 必须是 workflow 或 agent"
        aspec = dict(req.action_spec or {})
        if req.action_type == "workflow" and not aspec.get("workflow_id"):
            return False, "workflow 动作需要 workflow_id"
        if req.action_type == "agent" and not aspec.get("agent_id"):
            return False, "agent 动作需要 agent_id"

        now = int(time.time())
        next_run = 0
        if trigger_type == "schedule":
            next_run = self._next_schedule_time_raw(spec, now) or 0
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO tasks (name, org_id, robot_id, trigger_type, trigger_spec,
                       action_type, action_spec, enabled, state, next_run_at, expires_at,
                       created_at, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        req.name, req.org_id, req.robot_id, trigger_type,
                        json.dumps(spec, ensure_ascii=False),
                        req.action_type, json.dumps(aspec, ensure_ascii=False),
                        1 if req.enabled else 0,
                        STATE_IDLE if trigger_type == "schedule" else STATE_WAITING,
                        next_run, req.expires_at or 0, now, now,
                    ),
                )
                return True, cur.lastrowid

    async def update_task(self, task_id: int, req) -> Tuple[bool, str]:
        now = int(time.time())
        sets, vals = [], []
        for field in ("name", "robot_id"):
            v = getattr(req, field, None)
            if v is not None:
                sets.append(f"{field}=%s")
                vals.append(v)
        if req.trigger_spec is not None:
            sets.append("trigger_spec=%s")
            vals.append(json.dumps(req.trigger_spec, ensure_ascii=False))
        if req.action_spec is not None:
            sets.append("action_spec=%s")
            vals.append(json.dumps(req.action_spec, ensure_ascii=False))
        if req.expires_at is not None:
            sets.append("expires_at=%s")
            vals.append(req.expires_at)
        if req.enabled is not None:
            sets.append("enabled=%s")
            vals.append(1 if req.enabled else 0)
        # 触发配置或启用状态变化 → 用最新 spec 重算下次执行时间（schedule 且最终启用）
        if req.trigger_spec is not None or req.enabled is not None:
            task = await self.get_task(task_id)
            if task and task["trigger_type"] == "schedule":
                final_enabled = req.enabled if req.enabled is not None else bool(task["enabled"])
                if final_enabled:
                    spec = req.trigger_spec if req.trigger_spec is not None else {}
                    if req.trigger_spec is None:
                        try:
                            spec = json.loads(task["trigger_spec"] or "{}")
                        except Exception:
                            spec = {}
                    nxt = self._next_schedule_time_raw(spec, now) or 0
                    sets.append("next_run_at=%s")
                    vals.append(nxt)
        if not sets:
            return False, "没有可更新的字段"
        sets.append("updated_at=%s")
        vals.append(now)
        vals.append(task_id)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=%s", vals)
                return True, "ok"

    async def list_tasks(self, org_id: int = 0) -> List[dict]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                if org_id:
                    await cur.execute(
                        f"SELECT {_COL_LIST} FROM tasks WHERE org_id=%s ORDER BY id DESC",
                        (org_id,),
                    )
                else:
                    await cur.execute(f"SELECT {_COL_LIST} FROM tasks ORDER BY id DESC")
                rows = await cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def get_task(self, task_id: int) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_COL_LIST} FROM tasks WHERE id=%s", (task_id,)
                )
                row = await cur.fetchone()
        return self._row_to_task(row) if row else None

    async def toggle_task(self, task_id: int) -> bool:
        task = await self.get_task(task_id)
        if not task:
            return False
        now = int(time.time())
        enabled = 0 if task["enabled"] else 1
        nxt = 0
        new_state = task["state"]
        if enabled:
            if task["trigger_type"] == "schedule":
                try:
                    spec = json.loads(task["trigger_spec"] or "{}")
                except Exception:
                    spec = {}
                nxt = self._next_schedule_time_raw(spec, now) or 0
            # 重新启用已完成的一次性条件任务 → 复位为等待条件
            elif task["state"] == "done":
                new_state = STATE_WAITING
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE tasks SET enabled=%s, next_run_at=%s, state=%s, updated_at=%s WHERE id=%s",
                    (enabled, nxt, new_state, now, task_id),
                )
        return True

    async def delete_task(self, task_id: int) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM task_runs WHERE task_id=%s", (task_id,))
                await cur.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
        return True

    async def list_runs(self, task_id: int, limit: int = 50) -> List[dict]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_RUN_COLS} FROM task_runs WHERE task_id=%s "
                    "ORDER BY id DESC LIMIT %s",
                    (task_id, limit),
                )
                rows = await cur.fetchall()
        return [self._row_to_run(r) for r in rows]

    # ── 触发器内部查询 ─────────────────────────────

    async def _fetch_due_scheduled(self, now: int) -> List[dict]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_COL_LIST} FROM tasks WHERE trigger_type='schedule' "
                    "AND enabled=1 AND state<>'running' AND next_run_at>0 AND next_run_at<=%s",
                    (now,),
                )
                rows = await cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def _earliest_next_run(self) -> Optional[int]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT MIN(next_run_at) FROM tasks WHERE trigger_type='schedule' "
                    "AND enabled=1 AND next_run_at>0"
                )
                row = await cur.fetchone()
        return row[0] if row and row[0] else None

    async def _fetch_waiting_condition_tasks(self, robot_id: str) -> List[dict]:
        """取该设备上待触发的条件任务（含未过期的）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_COL_LIST} FROM tasks WHERE trigger_type='condition' "
                    "AND enabled=1 AND state IN ('waiting','idle') "
                    "AND (expires_at=0 OR expires_at>%s) "
                    "AND (robot_id=%s OR robot_id='')",
                    (int(time.time()), robot_id),
                )
                rows = await cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def _fetch_pending_friend_tasks(self, robot_id: str) -> List[dict]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_COL_LIST} FROM tasks WHERE trigger_type='condition' "
                    "AND enabled=1 AND state IN ('waiting','idle') AND robot_id=%s "
                    "AND (expires_at=0 OR expires_at>%s) "
                    "AND trigger_spec LIKE '%%friend_accepted%%'",
                    (robot_id, int(time.time())),
                )
                rows = await cur.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def _fetch_poll_needed_robots(self) -> List[str]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT DISTINCT robot_id FROM tasks WHERE trigger_type='condition' "
                    "AND enabled=1 AND state IN ('waiting','idle') AND robot_id<>'' "
                    "AND (expires_at=0 OR expires_at>%s) "
                    "AND trigger_spec LIKE '%%friend_accepted%%'",
                    (int(time.time()),),
                )
                rows = await cur.fetchall()
        return [self._row_to_robot_id(r) for r in rows]

    # ── 调度循环（定时任务） ───────────────────────

    async def _scheduler_loop(self):
        while self._running:
            try:
                now = int(time.time())
                due = await self._fetch_due_scheduled(now)
                for t in due:
                    await self._fire(t, f"定时（next_run_at={_fmt_ts(t['next_run_at'])}）",
                                     {"robot_id": t.get("robot_id")})
                await self._sync_pollers()
                nxt = await self._earliest_next_run()
                delay = SCHEDULER_SLEEP_IDLE
                if nxt:
                    delay = max(1.0, min(SCHEDULER_SLEEP_CAP, nxt - time.time()))
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("调度循环异常")
                await asyncio.sleep(SCHEDULER_SLEEP_IDLE)

    # ── 事件消费（条件触发） ───────────────────────

    def _on_device_event(self, event_type: str, payload: dict) -> None:
        """aivbot_service 事件回调（同步、非阻塞）：入队交事件消费者处理"""
        try:
            self._event_queue.put_nowait((event_type, payload))
        except Exception:
            logger.exception("任务事件入队失败")

    async def _event_consumer(self):
        while self._running:
            try:
                event_type, payload = await self._event_queue.get()
                await self._handle_event(event_type, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("任务事件处理异常")

    async def _handle_event(self, event_type: str, payload: dict) -> None:
        robot_id = str(payload.get("robot_id") or "")
        if event_type == "message_received" and robot_id:
            # 轻量信号：目标用户发来消息 → 唤醒轮询器做一次复核（friend_accepted 快路径）
            self._wake_poller(robot_id)
        tasks = await self._fetch_waiting_condition_tasks(robot_id)
        for t in tasks:
            try:
                spec = json.loads(t["trigger_spec"] or "{}")
                matched, ctx = await self._evaluate(t, spec, event_type, payload)
                if matched:
                    await self._fire(t, f"条件事件 {event_type}", ctx)
            except Exception:
                logger.exception(f"条件评估异常 task={t.get('id')}")

    def _wake_poller(self, robot_id: str) -> None:
        ev = self._poll_wake.get(robot_id)
        if ev is not None:
            ev.set()

    async def _evaluate(self, task: dict, spec: dict, event_type: str, payload: dict) -> Tuple[bool, dict]:
        """评估事件是否满足任务条件，返回 (是否命中, 上下文)"""
        ctype = spec.get("type")
        robot_id = str(payload.get("robot_id") or "")
        ctx = {"robot_id": robot_id, "event_type": event_type}

        if ctype == "message_from":
            if event_type != "message_received":
                return False, ctx
            target = str(spec.get("target") or "")
            room = str(payload.get("room") or "")
            if target and target == room:
                ctx.update({"room": room, "room_type": payload.get("room_type", 1)})
                return True, ctx
            return False, ctx

        if ctype == "friend_accepted":
            if event_type == "friend_info":
                friend = payload.get("friend") or {}
                if self._friend_match(spec, friend):
                    ctx.update({
                        "friend": friend,
                        "room": friend.get("name") or friend.get("markName") or "",
                        "room_type": 1,
                    })
                    return True, ctx
            elif event_type == "message_received":
                # 对方发来消息仅是"可能已通过"的信号，真实确认由共享轮询器完成：
                # 唤醒轮询器做一次 507 拉取比对（若命中会再次以 friend_info 事件触发）。
                room = str(payload.get("room") or "")
                target = spec.get("target") or {}
                tname = str(target.get("name") or target.get("markName") or "")
                if room and tname and room == tname:
                    self._wake_poller(robot_id)
            return False, ctx
        return False, ctx

    @staticmethod
    def _friend_match(spec: dict, friend: Any) -> bool:
        """target 与好友记录匹配：phone 精确；name/markName 支持包含匹配"""
        target = spec.get("target") or {}
        if isinstance(friend, str):
            blob = friend
            return bool(target.get("phone") and str(target["phone"]) in blob) or bool(
                target.get("name") and str(target["name"]) in blob
            )
        if not isinstance(friend, dict):
            return False
        phone = str(target.get("phone") or "")
        name = str(target.get("name") or "")
        mark = str(target.get("markName") or "")
        f_phone = str(friend.get("phone") or "")
        f_name = str(friend.get("name") or "")
        f_mark = str(friend.get("markName") or "")
        if phone and phone == f_phone:
            return True
        # 注意：空字符串是任意字符串的子串，必须保证双方非空再做包含匹配；
        # 只允许「目标名是好友名/备注的子串或相等」（如目标"张三"命中好友"张三丰"），
        # 反向（好友名是目标名的子串）会误匹配（如好友"张"命中目标"张三"）。
        if name:
            if (f_name and (name == f_name or name in f_name)) or (
                f_mark and (name == f_mark or name in f_mark)
            ):
                return True
        if mark:
            if (f_name and (mark == f_name or mark in f_name)) or (
                f_mark and (mark == f_mark or mark in f_mark)
            ):
                return True
        return False

    # ── 共享轮询器（friend_accepted 兜底） ─────────

    async def _sync_pollers(self) -> None:
        need = set(await self._fetch_poll_needed_robots())
        for rid in need:
            if rid not in self._poll_tasks or self._poll_tasks[rid].done():
                self._poll_tasks[rid] = asyncio.create_task(self._poller_loop(rid))
        for rid in list(self._poll_tasks):
            if rid not in need and not self._poll_tasks[rid].done():
                self._poll_tasks[rid].cancel()
                self._poll_tasks.pop(rid, None)
                self._poll_wake.pop(rid, None)

    def _device_online(self, robot_id: str) -> bool:
        try:
            conns = getattr(aivbot_service, "active_connections", None)
            return bool(conns and robot_id in conns)
        except Exception:
            return True

    async def _poller_loop(self, robot_id: str) -> None:
        """每设备一个共享轮询器：低频拉 507 全量好友，比对命中目标即触发。

        等待期间可被「对方发来消息」唤醒（快路径），空闲时 10 分钟一轮，带随机抖动。
        """
        wake = self._poll_wake.setdefault(robot_id, asyncio.Event())
        while self._running:
            try:
                pending = await self._fetch_pending_friend_tasks(robot_id)
                interval = POLL_INTERVAL_PENDING if pending else POLL_INTERVAL_IDLE
                jitter = random.uniform(0, interval * 0.1)
                try:
                    await asyncio.wait_for(wake.wait(), timeout=interval + jitter)
                except asyncio.TimeoutError:
                    pass
                wake.clear()
                if not self._running:
                    break
                pending = await self._fetch_pending_friend_tasks(robot_id)
                if not pending:
                    continue
                if not self._device_online(robot_id):
                    continue  # 设备离线：跳过本轮，不影响其他工作
                ok = await aivbot_service.send_command(robot_id, 507, {})
                if not ok:
                    await asyncio.sleep(10)
                    continue
                await self._wait_contacts_change(robot_id)
                await self._scan_contacts_for_targets(robot_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"条件轮询器异常 robot={robot_id}")
                await asyncio.sleep(30)

    async def _wait_contacts_change(self, robot_id: str) -> None:
        """等待 507 回执写入 received_data.contacts（签名比对，最长 POLL_CONTACTS_TIMEOUT 秒）"""
        def sig():
            return json.dumps(
                aivbot_service.get_received_data(robot_id).get("contacts"),
                ensure_ascii=False, default=str,
            )
        before = sig()
        for _ in range(int(POLL_CONTACTS_TIMEOUT / 0.5)):
            await asyncio.sleep(0.5)
            if sig() != before:
                return

    async def _scan_contacts_for_targets(self, robot_id: str) -> None:
        """扫描最新 contacts，命中待处理 friend_accepted 任务的目标即触发"""
        data = aivbot_service.get_received_data(robot_id).get("contacts") or []
        latest = data[-1] if isinstance(data, list) and data else data
        items = (latest or {}).get("items") or []
        raw = (latest or {}).get("raw") or ""
        tasks = await self._fetch_pending_friend_tasks(robot_id)
        for t in tasks:
            try:
                spec = json.loads(t["trigger_spec"] or "{}")
                hit = None
                for f in items:
                    if self._friend_match(spec, f):
                        hit = f
                        break
                if hit is None and raw:
                    hit = raw  # items 结构异常时退回全文匹配
                    if not self._friend_match(spec, hit):
                        hit = None
                if hit is not None:
                    room = hit.get("name") or hit.get("markName") or "" if isinstance(hit, dict) else ""
                    await self._fire(
                        t, "轮询确认（507 全量好友命中）",
                        {"robot_id": robot_id, "friend": hit, "room": room, "room_type": 1},
                    )
            except Exception:
                logger.exception(f"联系人比对异常 task={t.get('id')}")

    # ── 触发与执行 ────────────────────────────────

    async def trigger_now(self, task_id: int) -> Tuple[bool, str]:
        """手动触发一次任务（测试用）"""
        task = await self.get_task(task_id)
        if not task:
            return False, "任务不存在"
        ok = await self._fire(task, "手动触发", {"robot_id": task.get("robot_id")})
        return (True, "已触发") if ok else (False, "任务正在执行中（防重入），请稍后再试")

    async def _fire(self, task: dict, trigger_desc: str, ctx: dict) -> bool:
        """触发任务：原子占位防并发/重复触发，后台执行动作"""
        run_id = await self._start_run(task, trigger_desc)
        if run_id is None:
            return False

        async def _exec():
            try:
                result = await self._exec_action(task, ctx)
                await self._finish_run(run_id, "success", result)
                await self._after_run(task, "success")
            except Exception as e:
                logger.exception(f"任务执行异常 task={task['id']}")
                await self._finish_run(run_id, "failed", {"error": str(e)[:500]})
                await self._after_run(task, "failed")

        t = asyncio.create_task(_exec())
        self._exec_tasks.add(t)
        t.add_done_callback(self._exec_tasks.discard)
        return True

    async def _start_run(self, task: dict, trigger_desc: str) -> Optional[int]:
        """原子占位：同一任务不允许并发执行（幂等，防事件+轮询双重触发）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE tasks SET state='running', updated_at=%s WHERE id=%s AND state<>'running'",
                    (int(time.time()), task["id"]),
                )
                if cur.rowcount == 0:
                    return None
                await cur.execute(
                    "INSERT INTO task_runs (task_id, trigger_desc, status, started_at) "
                    "VALUES (%s,%s,'running',%s)",
                    (task["id"], trigger_desc, int(time.time())),
                )
                return cur.lastrowid

    async def _finish_run(self, run_id: int, status: str, result: Any) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE task_runs SET status=%s, result=%s, finished_at=%s WHERE id=%s",
                    (status, json.dumps(result, ensure_ascii=False, default=str)[:4000],
                     int(time.time()), run_id),
                )

    async def _after_run(self, task: dict, status: str) -> None:
        now = int(time.time())
        next_run = 0
        done_disable = False
        next_run = 0
        if task["trigger_type"] == "schedule":
            try:
                spec = json.loads(task["trigger_spec"] or "{}")
            except Exception:
                spec = {}
            # 单次定时（once_at）：执行一次即结束；重复次数耗尽（repeat_count）：结束后停止
            run_count = int(task.get("run_count") or 0) + 1
            repeat_count = int(spec.get("repeat_count") or 0)
            if spec.get("once_at") is not None or (repeat_count > 0 and run_count >= repeat_count):
                new_state, done_disable = STATE_DONE, True
            else:
                new_state = STATE_IDLE
                next_run = self._next_schedule_time_raw(spec, now) or 0
        else:
            # 条件任务：一次性（默认）成功后停用；失败保持可重试
            try:
                spec = json.loads(task["trigger_spec"] or "{}")
            except Exception:
                spec = {}
            if status == "success" and spec.get("oneshot", True):
                new_state, done_disable = STATE_DONE, True
            else:
                new_state = STATE_IDLE
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                if done_disable:
                    await cur.execute(
                        "UPDATE tasks SET state='done', enabled=0, run_count=run_count+1, "
                        "last_run_at=%s, last_result=%s, updated_at=%s WHERE id=%s",
                        (now, status, now, task["id"]),
                    )
                else:
                    await cur.execute(
                        "UPDATE tasks SET state=%s, run_count=run_count+1, last_run_at=%s, "
                        "last_result=%s, next_run_at=%s, updated_at=%s WHERE id=%s",
                        (new_state, now, status, next_run, now, task["id"]),
                    )

    # ── 动作执行 ──────────────────────────────────

    async def _exec_action(self, task: dict, ctx: dict) -> dict:
        action_type = task["action_type"]
        try:
            spec = json.loads(task["action_spec"] or "{}")
        except Exception:
            spec = {}
        robot_id = ctx.get("robot_id") or task.get("robot_id") or ""

        if action_type == "workflow":
            workflow_id = spec.get("workflow_id")
            if not workflow_id:
                raise ValueError("workflow 动作缺少 workflow_id")
            input_ctx = dict(spec.get("params") or {})
            input_ctx.update({"task_id": task["id"], **ctx})
            # 执行身份：spec.agent_id（智能体 ID）可选；缺省则 LLM/表格节点不可用
            agent = None
            aid = spec.get("agent_id")
            if aid:
                agent = await agent_service.get_agent(int(aid))
                if not agent:
                    raise ValueError(f"智能体不存在: {aid}")
            res = await workflow_engine.execute(
                int(workflow_id), robot_id, input_ctx, wait_device=True, agent=agent
            )
            return {
                "workflow_id": workflow_id,
                "status": res.get("status"),
                "output": res.get("output"),
                "error": res.get("error"),
                "run_id": res.get("run_id"),
            }

        if action_type == "agent":
            agent_id = spec.get("agent_id")
            instruction = str(spec.get("instruction") or "")
            if not agent_id:
                raise ValueError("agent 动作缺少 agent_id")
            agent = await agent_service.get_agent(int(agent_id))
            if not agent:
                raise ValueError(f"智能体不存在: {agent_id}")
            # 发送回复三态：none=不发送 / source=回复到触发事件会话 / custom=回复到自定义会话。
            # 兼容旧数据：布尔 True→custom（room 有值）/ source（room 为空），False/缺省→none
            #
            # 回复由智能体自己通过设备指令（203）发出，服务器不再代发它的文本输出；
            # 三态改为注入给 LLM 的回复规则（见 generate_reply 的 reply_policy）：
            # none → 明确要求本次不要发送任何消息；source/custom → 要求回复到下面的会话名。
            sr = spec.get("send_reply", "none")
            if isinstance(sr, (int, float)):
                send_mode = ("custom" if spec.get("room") else "source") if sr else "none"
            else:
                v = str(sr).strip().lower()
                if v in ("true", "1", "yes", "on"):
                    send_mode = "custom" if spec.get("room") else "source"
                elif v in ("none", "source", "custom"):
                    send_mode = v
                else:
                    send_mode = "none"
            if send_mode == "custom":
                room = str(spec.get("room") or "")
            elif send_mode == "source":
                room = str(ctx.get("room") or "")
            else:
                # none：会话照常按旧规则建立（仅不发送回复）
                room = str(spec.get("room") or ctx.get("room") or "")
            room_type = int(spec.get("room_type", 1))
            reply = await agent_service.generate_reply(
                agent, robot_id=robot_id, room_name=room,
                room_type=room_type, user_text=instruction,
                # 群聊要让模型知道"机器人自己的企微名"，否则客户 @ 它时会被判成"不是对我说的"
                self_name=aivbot_service.get_self_name(robot_id),
                reply_policy="none" if send_mode == "none" else "reply",
            )
            if not reply:
                # generate_reply 仅在 LLM 调用异常/返回空内容时返回 None
                # （工具执行错误已作为文本回传，不影响回复）。此处必须抛错让任务
                # 记为失败，否则"已成功"会掩盖真实失败（如表格未写入）。
                # 注：回复规则已要求模型"完成操作后必须用一句话说明"，正常成功不会被判失败。
                raise ValueError(
                    "智能体回复生成失败（LLM 调用异常或返回空内容），动作未完成，请查看服务端日志"
                )
            return {"agent_id": agent_id, "reply": reply}

        raise ValueError(f"未知动作类型: {action_type}")

    @staticmethod
    def _next_schedule_time_raw(spec: dict, after_ts: float) -> Optional[int]:
        """计算定时任务在 after_ts 之后的下一次执行时间。
        spec 支持：cron（可带 offset_sec 延迟开始）、interval_sec（可带 offset_sec）、
        once_at（单次：时间戳，已过期返回 None 不再排期）"""
        offset = int(spec.get("offset_sec") or 0)
        if spec.get("cron"):
            return next_cron_time(str(spec["cron"]), after_ts + offset)
        if spec.get("interval_sec"):
            return int(after_ts + offset + int(spec["interval_sec"]))
        once = spec.get("once_at")
        if once is not None:
            once = int(once)
            return once if once > after_ts else None  # 已过期：不再排期
        return None


# 全局单例（与 services/__init__.py 保持同一实例）
task_service = TaskService()
