"""
Aivbot business logic service
"""

import asyncio
import json
import logging
import time
import uuid
from collections import Counter, deque
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from fastapi import WebSocket
from ..models.message import MessageType

# 房间名不可能是这些页面标签/占位词（设备端读错标题时会出现；纯数字则是未读数）
_SUSPICIOUS_ROOM_NAMES = {
    "群聊", "聊天", "全部", "联系人", "面聊", "聊天记录", "邮件", "文档", "日程", "微盘", "应用与功能",
}


from .agent_service import mentions_me

logger = logging.getLogger(__name__)


# ── v2 自动回复默认配置（与 robot_service.DEFAULT_CONFIG 一致；未绑定策略组时兜底）──
_DEFAULT_TYPE_CONFIG = {
    "1": {"mode": "at_only", "merge_window": 4, "track_duration": 180},  # 外部群
    "2": {"mode": "reply", "merge_window": 4},                            # 外部私聊
    "3": {"mode": "at_only", "merge_window": 4, "track_duration": 180},  # 内部群
    "4": {"mode": "reply", "merge_window": 4},                            # 内部私聊
}
_DEFAULT_LIMITS = (2, 5)  # (max_concurrent_llm, max_calls_per_min)
_QUEUE_MAXLEN = 50  # 每队列消息上限，防异常积压
_STRATEGY_CACHE_TTL = 5.0
_ROBOT_CACHE_TTL = 3.0


def build_command_extra(extra: dict) -> dict:
    """设备指令参数归一化（下发前的最终整形）：

    - 列表字段（titleList/selectList/nameList/removeList/atList 等）逗号字符串 → 数组
    - friend_* 平铺字段包装进嵌套 friend 对象（设备端 WeworkMessageBean.friend）
    - mark_name（213 修改好友备注用法）→ friend.markName
    - 剔除 UI 元数据（usage：213 节点表单的「用法」选择）

    设备端把消息解析为 WeworkMessageBean，好友相关数据都在 friend 对象里
    （friend.phone/name/markName/markCorp/markExtra/leavingMsg）。平铺字段会被设备
    忽略导致 message.friend=null，如 213 报错 "message.friend must not be null"。
    """
    LIST_KEYS = {
        "titleList", "selectList", "nameList", "removeList", "atList",
        "friendList", "messageList",
    }
    out: Dict[str, Any] = {}
    friend: Dict[str, Any] = {}
    for k, v in extra.items():
        if v is None:
            continue
        if k == "usage":
            continue
        if k == "mark_name":
            friend["markName"] = v
        elif k.startswith("friend_"):
            friend[k[len("friend_"):]] = v
        elif k in LIST_KEYS:
            if isinstance(v, str):
                out[k] = [s.strip() for s in v.split(",") if s.strip()]
            elif isinstance(v, list):
                out[k] = v
            else:
                out[k] = v
        else:
            out[k] = v
    if friend:
        out["friend"] = friend
    return out


def device_send_attempted(trace: Optional[list]) -> bool:
    """trace（generate_reply 的工具调用轨迹）里是否出现过「可能已把消息发给客户」的指令。

    回复由智能体自己通过设备指令发出（服务器不代发），因此：
    - 日志要能区分「模型判定不回」/「已通过设备指令回复」/「模型没照做」；
    - 出错重试时要靠它判断，避免模型已经发出后重投 → 客户收到两条。

    203=发送消息 / 204=指定回复(@) / 205=转发消息；下发超时按「可能已发出」保守处理。
    """
    for t in trace or []:
        tool = str(t.get("tool") or "")
        if not tool.startswith("device_command_"):
            continue
        try:
            cmd = int(tool.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            continue
        if cmd not in (203, 204, 205):
            continue
        result = str(t.get("result") or "")
        if result.lstrip().startswith("错误") or "下发异常" in result:
            continue  # 授权/参数/离线校验失败，未下发
        if "已下发" in result or "设备指令执行结果" in result or "模拟" in result:
            return True
    return False


def log_task_exception(task: "asyncio.Task") -> None:
    """后台任务完成回调：任务异常必须留痕。

    背景：`task.add_done_callback(set.discard)` 只是把任务从集合里摘掉，从不调用
    `task.exception()` —— 异常因此永远无人认领，只在任务对象被 GC 时由 asyncio
    打一行默认告警。技能触发链路就曾这样静默失效数天。
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            f"后台任务异常（未捕获，链路可能已静默降级）: {task.get_name()}",
            exc_info=exc,
        )


def install_loop_exception_handler(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """兜底：任何未被认领的异步异常都打完整堆栈，不再只留一行默认输出。

    默认处理器只把 "Task exception was never retrieved" 打到 stderr，服务端跑在
    uvicorn 下很容易被忽略；这里统一走 logging（含堆栈与任务来源）。
    """
    loop = loop or asyncio.get_running_loop()
    default = loop.get_exception_handler()

    def _handler(ev_loop, context):
        msg = context.get("message") or "未处理的异步异常"
        exc = context.get("exception")
        task = context.get("task")
        where = f" task={task.get_name()}" if task is not None else ""
        if exc is not None:
            logger.error(f"未处理的异步异常: {msg}{where}", exc_info=exc)
        else:
            logger.error(f"未处理的异步异常: {msg}{where} {context.get('future') or ''}")
        if default is not None:
            default(ev_loop, context)

    loop.set_exception_handler(_handler)


class AivbotService:
    """Service for handling Aivbot business logic"""

    def __init__(self):
        # Active WebSocket connections
        self.active_connections: Dict[str, WebSocket] = {}

        # Message statistics
        self.message_count = 0
        self.connection_count = 0

        # Stored received data per client (for test page display)
        self.received_data: Dict[str, Dict[str, Any]] = {}

        # Dedup: track last message hash per (client_id, room) for type 101
        self._last_msg_hash: Dict[str, str] = {}

        # Track last seen message fingerprint counts per room (compute newly-added messages)
        self._last_seen_counts: Dict[str, Counter] = {}

        # 已确认过的房间类型纠偏（key=f"{client_id}|{room}"）：设备端标题时读得到时读不到，
        # 只认到一次「外部群」就记住，避免后续上报又退回 room_type=3 命中 silent 策略。
        self._room_type_override: Dict[str, int] = {}

        # ── v2 自动回复引擎状态 ──
        # 成员级回复队列：key=私聊 "{client_id}|{room}"；群聊 "{client_id}|{room}|{speaker}"
        # value = {msgs: deque(maxlen), timer: Task|None, processing: bool, room, room_type, cfg, retries}
        self._reply_queues: Dict[str, dict] = {}
        # 追踪@状态：(room, speaker) -> 追踪到期时间戳（群聊 track_at 模式）
        self._track_state: Dict[Tuple[str, str], float] = {}
        # 设备级成本控制：并发调用数与 60s 调用时间戳
        self._llm_active: Dict[str, int] = {}
        self._llm_call_times: Dict[str, List[float]] = {}
        # 策略/设备配置缓存（TTL 短，管理员改动快速生效）
        self._strategy_cache: Dict[str, Tuple[float, Optional[Any]]] = {}
        self._robot_cache: Dict[str, Tuple[float, Optional[Any]]] = {}

        # 后台异步回复任务（防止 GC 回收，任务完成时自动移除）
        self._bg_tasks: set = set()

        # 设备事件监听器（任务模块条件引擎注册：收到消息/好友上报等事件时回调）
        self._event_listeners: list = []

        # Task status per device: True=正在接收消息(202已启动) False=已停止(201)
        # 由设备端 type=512 上报为准，服务端下发 202/201 时乐观同步
        self.task_status: Dict[str, bool] = {}

    def _track_bg_task(self, task: "asyncio.Task") -> None:
        """登记后台任务：完成时既摘出集合、也把异常打进日志（见 log_task_exception）"""
        self._bg_tasks.add(task)
        task.add_done_callback(log_task_exception)
        task.add_done_callback(self._bg_tasks.discard)

    async def connect(self, websocket: WebSocket, client_id: str):
        """Accept and register WebSocket connection (multi-connection per client).

        一个 client_id 允许多个连接并存（设备重连/旧连接残留时不会互相覆盖），
        只有所有连接都断开后该设备才从注册表消失。
        """
        await websocket.accept()
        self.active_connections.setdefault(client_id, []).append(websocket)
        self.connection_count += 1
        # 新连接默认视为「已停止」：设备重连/App 重启时旧 task_status 可能残留 True，
        # 会导致工作流引擎误补发 202 把已停止的设备打开。设备连接后会上报 512
        # （WeworkService 连接建立即上报当前任务状态），以设备上报为准纠正。
        self.task_status[client_id] = False
        logger.info(
            f"✅ Client connected: {client_id} | Total: {len(self.active_connections)}"
        )

    def disconnect(self, client_id: str, websocket: WebSocket = None):
        """Remove WebSocket connection (multi-connection per client).

        Only removes the exact `websocket` instance from the client's connection
        list — a stale/old connection's finally-block can never remove a newer
        connection of the same robot_id. The client disappears from the registry
        only when its last connection is gone.
        """
        conns = self.active_connections.get(client_id)
        if websocket is not None:
            if conns is not None:
                try:
                    conns.remove(websocket)
                    logger.info(
                        f"🔌 移除连接: {client_id}（该设备剩余 {len(conns)} 个连接）"
                    )
                except ValueError:
                    pass
                if not conns:
                    self.active_connections.pop(client_id, None)
                    logger.info(f"🔌 设备 {client_id} 已无连接，从注册表移除")
        elif conns is not None:
            self.active_connections.pop(client_id, None)
            logger.info(f"🔌 设备 {client_id} 已从注册表移除（无实例指定）")
        # 仅当该设备完全无连接时才清除任务状态
        if client_id not in self.active_connections:
            self.task_status.pop(client_id, None)

    async def send_message(self, client_id: str, message: dict):
        """Send message to specific client (try every live connection)."""
        conns = self.active_connections.get(client_id)
        if not conns:
            logger.warning(
                f"⚠️ Client not found: {client_id} | 当前已连接: {list(self.active_connections.keys())}"
            )
            return False

        # 遍历该设备全部连接，任一发送成功即算成功（多连接并存时避免误报离线）
        for websocket in list(conns):
            try:
                await websocket.send_json(message)
                logger.debug(f"📤 Message sent to {client_id}")
                return True
            except Exception as e:
                logger.error(f"❌ Failed to send message: {e}")
                self.disconnect(client_id, websocket)
        return False

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients"""
        for client_id, conns in list(self.active_connections.items()):
            for websocket in list(conns):
                try:
                    await websocket.send_json(message)
                except Exception as e:
                    logger.error(f"❌ Broadcast failed for {client_id}: {e}")
                    self.disconnect(client_id, websocket)

    # ── Send command to device ─────────────────────────────

    async def send_command(
        self, client_id: str, cmd_type: int, extra: dict = None
    ) -> bool:
        """
        Send a command to Aivbot device.

        Args:
            client_id: Robot ID
            cmd_type: Command type (203=SEND, 501=GET_GROUP_INFO, 507=GET_ALL_FRIEND, etc.)
            extra: Additional fields to include in the command
        """
        # 同步任务状态：202=开始接收 201=停止（仅发送成功才更新，最终以设备端 type=512 上报为准）
        msg_id = (
            f"server_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
        )
        cmd = {
            "socketType": 2,
            "messageId": msg_id,
            "encryptType": 0,
            "list": [{"type": cmd_type, **(extra or {})}],
        }
        logger.info(
            f"📤 [SEND_CMD] {client_id} -> {json.dumps(cmd, ensure_ascii=False)}"
        )
        ok = await self.send_message(client_id, cmd)
        if ok:
            if cmd_type == 202:
                self.task_status[client_id] = True
            elif cmd_type == 201:
                self.task_status[client_id] = False
        return ok

    # ── Handle incoming data ───────────────────────────────

    async def handle_message(self, data: dict, client_id: str):
        """Handle incoming message from client"""
        # td: device transport probe {"td": timestamp}, no business meaning
        if "td" in data:
            return

        # Heartbeat: device sends bare {"type": 11}
        if data.get("type") == MessageType.HEART_BEAT:
            logger.debug(f"💓 Heartbeat from {client_id}")
            await self.send_message(
                client_id,
                {
                    "type": MessageType.HEART_BEAT,
                    "timestamp": datetime.now().isoformat(),
                },
            )
            return

        socket_type = data.get("socketType")

        # RAW_CONFIRM (3) — execution result from a command
        if socket_type == 3:
            await self._handle_raw_confirm(data, client_id)
            return

        # MESSAGE_LIST (2) — message list or data upload from device
        if socket_type == 2:
            await self._handle_list_data(data, client_id)
            return

        # Unknown
        logger.info(f"📨 Unknown message: {json.dumps(data, ensure_ascii=False)[:200]}")

    async def _handle_list_data(self, data: dict, client_id: str):
        """Handle list data from Aivbot (socketType=2)"""
        # Aivbot may send items in 'list' or 'messageList' field
        items = data.get("list") or data.get("messageList") or []
        raw_list = data.get("list") or []  # prefer 'list' for detection

        if not items:
            logger.warning(f"Empty list from {client_id}")
            return

        self.message_count += len(items)
        logger.info(
            f"📬 Received {len(items)} item(s) from {client_id} | Total: {self.message_count}"
        )

        for item in items:
            await self._process_item(item, client_id)

    async def _process_item(self, item: dict, client_id: str):
        """Process a single item from device"""
        item_type = item.get("type", 0)
        now_str = datetime.now().strftime("%H:%M:%S")

        # ── Chat messages (type=101 = upload message list) ──
        if item_type == 101:  # TYPE_RECEIVE_MESSAGE_LIST
            msg_list = item.get("messageList") or []
            title_list = item.get("titleList") or []
            room_type = item.get("roomType", 0)
            source = "群聊" if room_type in (1, 3) else "单聊"
            room = title_list[0] if title_list else ""
            # 房间名像是"未读数"或页面占位词 → 设备端读错了标题（真机踩过：房间名成了 "1"，
            # LLM 回复时 titleList="1"，设备进不了任何房间）。这里只告警：能修的是设备端读取，
            # 服务端此时既不知道真名，也不该干脆不回（那同样丢客户消息）。
            if room and (room.isdigit() or room in _SUSPICIOUS_ROOM_NAMES):
                logger.warning(
                    f"  ⚠️ 房间名可疑 {client_id}: room={room!r} titleList={title_list} "
                    f"room_type={room_type}（疑似未读数/占位名，设备端标题读取可能出错）"
                )
            # ── 设备端房间类型纠偏 ──
            # 客户端 WeworkRoomUtil.isExternalGroup() 曾用 findFrontNode(listView) 找「外部群」
            # 字样，与 getRoomTitle() 用的 findFrontNode(list.parent?.parent) 不是同一棵子树，
            # 导致外部群被判成内部群(3)（标题里的 "群名(9)" 满足了 isGroupChat），
            # 而内部群在策略里常配 silent → 群里 @ 机器人也永远不回复。
            # 设备上报的 titleList 里本来就有「外部群」这个事实，这里按它纠正，并记住结果
            # （标题不是每次都能读到：读不到时会退回 3，那就又命中 silent 了）。
            # ponytail: 兜底而已；客户端修好（见 comvbot WeworkRoomUtil.isExternalGroup）后可删。
            override_key = f"{client_id}|{room}"
            if room_type in (1, 3):
                known = self._room_type_override.get(override_key)
                if known is not None:
                    room_type = known
                elif room_type == 3 and any("外部群" in str(t) for t in title_list[1:]):
                    logger.info(
                        f"  🩹 设备上报 room_type=3，但标题含「外部群」→ 按外部群(1) 处理: {room}"
                    )
                    room_type = 1
                    self._room_type_override[override_key] = 1
                source = "群聊" if room_type in (1, 3) else "单聊"
            # ── 任务模块事件：收到消息（条件触发 message_from / friend_accepted 信号用） ──
            self._emit_event(
                "message_received",
                robot_id=client_id,
                room=room,
                room_type=room_type,
            )

            # extract all message texts with sender info, skip timestamps (feature=0)
            # v2：群聊附加发言者 nameList（成员级队列路由用）
            is_group_upload = room_type in (1, 3)
            texts = []
            for m in msg_list:
                sender = m.get(
                    "sender", -1
                )  # -1=未设置, 0=其他人, 1=机器人自己, 2=系统
                if sender != -1:
                    logger.info(f"  📨 sender={sender}")
                speaker = ""
                if is_group_upload:
                    nl = m.get("nameList") or []
                    speaker = nl[0] if nl else ""
                # 图片消息：带 imagePath 时记录图片路径（image=True），文本/图片分开处理
                image_path = m.get("imagePath")
                if image_path:
                    texts.append(
                        {
                            "text": image_path,
                            "sender": sender,
                            "image": True,
                            "speaker": speaker,
                        }
                    )
                    continue
                parts = m.get("itemMessageList") or []
                for p in parts:
                    if p.get("feature", 0) != 0 and p.get("text"):
                        texts.append(
                            {
                                "text": p.get("text", ""),
                                "sender": sender,
                                "speaker": speaker,
                            }
                        )

            last_text = texts[-1]["text"] if texts else ""
            msg_count = len(texts)

            # Dedup: skip if same message list already stored
            import hashlib

            dedup_key = f"{client_id}|{room}"
            content_hash = hashlib.md5(
                "|".join(t["text"] for t in texts).encode()
            ).hexdigest()
            if self._last_msg_hash.get(dedup_key) == content_hash:
                logger.debug(f"  ⏭ Skip dup: {room}")
                return
            self._last_msg_hash[dedup_key] = content_hash

            # 计算本次“新增”的消息：设备每次上报的是房间完整消息列表，
            # 但列表会变长/变短/重排（自己刚发的消息插入、列表被重读、超出容量被截断）。
            # 旧实现「前缀一致且变长才取后缀」在列表变短时会把整批当新消息 → 同一条用户
            # 消息被重复提交 LLM、重复回复。这里改成多重集差集：按 (发送者, 发言者, 文本)
            # 计数做减法，多出来的就是新增，且同文本连发两条不会被吞。
            prev_counts = self._last_seen_counts.get(dedup_key)
            cur_counts = Counter(
                (t.get("sender"), t.get("speaker") or "", t.get("text") or "")
                for t in texts
            )
            if prev_counts:
                remaining = cur_counts - prev_counts
                new_texts = []
                for t in texts:
                    fp = (t.get("sender"), t.get("speaker") or "", t.get("text") or "")
                    if remaining.get(fp, 0) > 0:
                        remaining[fp] -= 1
                        new_texts.append(t)
            else:
                # 该房间首次上报（服务端刚重启、或第一次看到这个房间）：整窗都是历史，
                # 没法区分新旧 —— 只把最后一条当作"刚到的消息"，其余仅用于建立基线。
                # 否则窗口里翻出来的旧消息会被当成新消息：旧的 @ 会重新开启 track_at 追踪
                # （表现为"没人@也回复"），旧消息本身也会被重复回复。
                new_texts = texts[-1:]
            self._last_seen_counts[dedup_key] = cur_counts

            speaker_desc = ""
            if is_group_upload:
                sps = sorted({t.get("speaker") or "" for t in texts})
                speaker_desc = " 发言者=" + (
                    ", ".join(s if s else "（未解析）" for s in sps) if sps else "（无）"
                )
            logger.info(
                f"  💬 [{source}] {room}(room_type={room_type}): {msg_count}条消息, "
                f"最新: {last_text[:60]}{speaker_desc}"
            )

            # ── 自动回复 v2（后台异步，不阻塞接收循环）──
            # 策略判断 + 成员级入队 + 合并计时器。LLM 调用在窗口到期后异步执行，
            # 本处只做路由，避免策略查询/入队阻塞该设备 WebSocket 接收循环。
            other_new = [t for t in new_texts if t.get("sender") == 0]
            if other_new:
                task = asyncio.create_task(
                    self._route_auto_reply(
                        client_id=client_id,
                        room=room,
                        room_type=room_type,
                        new_texts=other_new,
                    )
                )
                self._track_bg_task(task)

            self._store(
                client_id,
                "recent_messages",
                {
                    "time": now_str,
                    "type": source,
                    "room": room,
                    "msg_count": msg_count,
                    "latest": last_text,
                    "all_texts": texts,
                    "raw": item,
                },
                maxlen=50,
            )

        # ── GET_ALL_FRIEND_INFO result ──
        elif item_type == 507:
            name_list = item.get("nameList") or item.get("titleList") or []
            count = len(name_list)
            logger.info(f"  👥 Contacts: {count} names")
            self._store(
                client_id,
                "contacts",
                {
                    "time": now_str,
                    "type": "全部好友",
                    "count": count,
                    "items": name_list,
                    "raw": item,
                },
            )

        # ── GET_ALL_GROUP_INFO result ──
        elif item_type == 508:
            name_list = item.get("titleList") or []
            logger.info(f"  👥 Groups: {len(name_list)} groups")
            self._store(
                client_id,
                "groups",
                {
                    "time": now_str,
                    "type": "全部群",
                    "count": len(name_list),
                    "items": name_list,
                    "raw": item,
                },
            )

        # ── GET_GROUP_INFO result (single group) ──
        elif item_type == 501:
            group_name = item.get("groupName", "")
            members = item.get("nameList") or []
            logger.info(f"  👥 Group info: {group_name} ({len(members)} members)")
            self._store(
                client_id,
                "group_info_detail",
                {
                    "time": now_str,
                    "group_name": group_name,
                    "owner": item.get("groupOwner", ""),
                    "member_count": item.get("groupNumber", len(members)),
                    "members": members,
                    "announcement": item.get("groupAnnouncement", ""),
                    "remark": item.get("groupRemark", ""),
                    "raw": item,
                },
                maxlen=200,
                dedup_field="group_name",
            )

        # ── GET_FRIEND_INFO result (单个好友详情 / 自动通过好友上报) ──
        elif item_type == 502:
            friend = item.get("friend") or {}
            # ── 任务模块事件：好友资料 / 自动通过好友上报（friend_accepted 条件触发用） ──
            self._emit_event("friend_info", robot_id=client_id, friend=friend)
            logger.info(
                f"  👤 Friend info: {friend.get('name', '?')} "
                f"phone={friend.get('phone', '')} "
                f"markName={friend.get('markName', '')} "
                f"leavingMsg={friend.get('leavingMsg', '')}"
            )
            self._store(
                client_id,
                "friend_info",
                {
                    "time": now_str,
                    "friend": friend,
                    "raw": item,
                },
                maxlen=20,
            )

        # ── GET_RECENT_LIST result ──
        elif item_type == 505:
            msg_list = item.get("messageList") or []
            logger.info(f"  📋 Recent list: {len(msg_list)} conversations")
            self._store(
                client_id,
                "recent_list",
                {
                    "time": now_str,
                    "count": len(msg_list),
                    "items": msg_list,
                    "raw": item,
                },
            )

        # ── GET_MY_INFO result ──
        elif item_type == 503:
            my_info = item.get("myInfo") or {}
            logger.info(f"  👤 My info: {my_info.get('name', '?')}")
            self._store(
                client_id, "my_info", {"time": now_str, "info": my_info, "raw": item}
            )

        # ── GET_CORP_LIST result ──
        elif item_type == 506:
            corp_list = item.get("titleList") or []
            logger.info(f"  🏢 Corps: {corp_list}")
            self._store(
                client_id,
                "corp_list",
                {"time": now_str, "corps": corp_list, "raw": item},
            )

        # ── TASK_STATUS_UPLOAD (512) 设备任务状态上报 ──
        elif item_type == 512:
            status = item.get("taskStatus")
            running = status == 1
            self.task_status[client_id] = running
            logger.info(
                f"  📡 任务状态上报 {client_id}: {'🔴 正在接收消息' if running else '⏹ 已停止'}"
            )

        # ── 设备端错误日志（type=302，客户端 error()/log() 上报）──
        # 以前直接落进 else 只写服务端 stdout，真机排查"进不去房间 / 发不出消息"时
        # 只能看到一句"发送失败"，看不到客户端到底卡在哪一步 —— 存下来给功能测试页看。
        elif item_type == 302:
            msg = str(item.get("log") or item.get("message") or "")[:500]
            if msg:
                logger.warning(f"  🚨 设备错误日志 {client_id}: {msg}")
                self._store(client_id, "device_log", {"time": now_str, "log": msg})

        # ── Other types ──
        else:
            logger.info(
                f"  📦 type={item_type}: {json.dumps(item, ensure_ascii=False)[:150]}"
            )

    # ── 自动回复 v2：成员级队列 + 合并窗口 + 追踪@ + 设备级成本控制 ──

    @staticmethod
    def _type_config(strategy, room_type: int) -> dict:
        """取某聊天室类型的策略配置；未绑定策略组/缺配置时回退默认"""
        if strategy is not None:
            cfg = (strategy.config or {}).get(str(room_type))
            if isinstance(cfg, dict):
                return cfg
        return _DEFAULT_TYPE_CONFIG.get(str(room_type), {"mode": "silent", "merge_window": 4})

    @staticmethod
    def _queue_key(client_id: str, room: str, speaker: str = "") -> str:
        return f"{client_id}|{room}" if not speaker else f"{client_id}|{room}|{speaker}"

    @staticmethod
    def _parse_queue_key(key: str) -> Tuple[str, str, str]:
        parts = key.split("|")
        client_id = parts[0]
        room = parts[1] if len(parts) > 1 else ""
        speaker = "|".join(parts[2:]) if len(parts) > 2 else ""
        return client_id, room, speaker

    async def _get_strategy_cached(self, client_id: str):
        """读取设备绑定的回复策略组（短 TTL 缓存，管理员改动快速生效）"""
        now = time.time()
        hit = self._strategy_cache.get(client_id)
        if hit and now - hit[0] < _STRATEGY_CACHE_TTL:
            return hit[1]
        strategy = None
        try:
            from ..services.robot_service import robot_service as _robot_service

            strategy = await _robot_service.get_robot_strategy(client_id)
        except Exception as e:
            logger.warning(f"读取设备回复策略失败 {client_id}: {e}")
        self._strategy_cache[client_id] = (now, strategy)
        return strategy

    async def _get_robot_cached(self, client_id: str):
        """读取设备配置（自动回复主开关 + 绑定智能体），短 TTL 缓存"""
        now = time.time()
        hit = self._robot_cache.get(client_id)
        if hit and now - hit[0] < _ROBOT_CACHE_TTL:
            return hit[1]
        robot = None
        try:
            from ..services.robot_service import robot_service as _robot_service

            robot = await _robot_service.get_robot(client_id)
        except Exception as e:
            logger.warning(f"读取设备配置失败 {client_id}: {e}")
        self._robot_cache[client_id] = (now, robot)
        return robot

    async def _route_auto_reply(
        self,
        client_id: str,
        room: str,
        room_type: int,
        new_texts: List[dict],
    ):
        """自动回复路由：策略判断 → 成员级入队 → 启动/重置合并计时器。
        由 _process_item 后台调用，不阻塞 WebSocket 接收循环。"""
        try:
            is_group = room_type in (1, 3)
            strategy = await self._get_strategy_cached(client_id)
            cfg = self._type_config(strategy, room_type)
            mode = cfg.get("mode", "at_only" if is_group else "reply")
            if mode == "silent":
                # 必须留痕：这个分支不打日志时，"消息收到了但什么也没发生"完全无法排障
                # （群里 @了机器人却不回复，最常见的原因就是 room_type 落到 silent 那一档）
                logger.info(
                    f"  🔇 策略为不回复(silent)，消息不入队: {room}（room_type={room_type}）"
                )
                return

            # 设备主开关（管理页「推送 LLM」）：关闭 = 设备级静默，不入队不推送
            robot = await self._get_robot_cached(client_id)
            if robot is not None and not robot.auto_reply:
                logger.info(f"  🚫 自动回复主开关已关闭（不推送队列）: {room}")
                return

            window = int(cfg.get("merge_window", 4))
            track_duration = int(cfg.get("track_duration", 180))
            # 本机在企业微信里的名字（用于判断"是否真的 @ 了我"）。
            # 设备上报的 101 事件不含 at/atList，只能从正文认；名字来自 503 缓存，
            # 拿不到时 mentions_me 会退回旧的"含 @ 就算"（不退化）。
            self_name = self.get_self_name(client_id)
            # 策略组全局成员黑名单（精确匹配群聊发言者名）：命中不入队不调 LLM
            blocklist = []
            if strategy is not None and isinstance(strategy.config, dict):
                bl = strategy.config.get("member_blocklist") or []
                blocklist = [str(x).strip() for x in bl if str(x).strip()]
            for entry in new_texts:
                mentioned = mentions_me(entry.get("text", ""), self_name)
                speaker = entry.get("speaker") or ""
                if speaker and speaker in blocklist:
                    logger.info(f"  🚫 成员黑名单拦截（不入队）: {room}/{speaker}")
                    continue
                if is_group:
                    # 追踪@：含 @ 我 → 该成员进入追踪状态（已在追踪则重置）
                    if mode == "track_at" and mentioned:
                        self._track_state[(room, speaker)] = time.time() + track_duration
                        if not speaker:
                            # 群成员昵称没解析出来（设备端 nameList 为空）时，队列 key 里也没有
                            # speaker，此时必须同时登记"房间级"追踪标记，否则 _is_tracked 永远
                            # 为 False —— @了机器人也永远不回复。
                            self._track_state[(room, "")] = time.time() + track_duration
                            logger.info(
                                f"  🎯 追踪@命中但未解析出成员昵称，按房间级追踪: {room}"
                            )
                    key = self._queue_key(client_id, room, speaker)
                else:
                    key = self._queue_key(client_id, room)
                # 仅@回复：不含 @ 的消息不入队（入队后仍走合并计时器）
                if mode == "at_only" and not mentioned:
                    continue
                self._enqueue(key, room, room_type, cfg, entry)
        except Exception as e:
            logger.error(f"  ❌ 自动回复路由异常 robot={client_id} room={room}: {e}")

    def _enqueue(self, key: str, room: str, room_type: int, cfg: dict, entry: dict):
        """入队并启动/重置合并计时器（处理中只入队不启计时器）"""
        q = self._reply_queues.get(key)
        if q is None:
            q = {
                "msgs": deque(maxlen=_QUEUE_MAXLEN),
                "timer": None,
                "processing": False,
                "room": room,
                "room_type": room_type,
                "cfg": cfg,
                "retries": 0,
            }
            self._reply_queues[key] = q
        q["msgs"].append(entry)
        if q["processing"]:
            return  # 当前正在调 LLM：新消息积压，结束后再启新窗口
        if q["timer"] is not None:
            # 重置：cancel 后立即清引用，避免旧任务尚未处理取消导致 _restart_timer 误判
            q["timer"].cancel()
            q["timer"] = None
        self._restart_timer(key, q, int(cfg.get("merge_window", 4)))

    def _restart_timer(self, key: str, q: dict, window: int):
        """为队列启动一个新的合并倒计时（已有计时器则忽略）"""
        if q["timer"] is not None:
            return
        task = asyncio.create_task(self._queue_timer(key, q, window))
        q["timer"] = task
        self._track_bg_task(task)

    async def _queue_timer(self, key: str, q: dict, window: int):
        """合并窗口倒计时：到期取整批提交 LLM（可被重置取消）"""
        task = asyncio.current_task()
        try:
            await asyncio.sleep(window)
            if q.get("timer") is task:
                q["timer"] = None
            await self._process_queue(key, q)
        except asyncio.CancelledError:
            if q.get("timer") is task:
                q["timer"] = None
            raise

    async def _process_queue(self, key: str, q: dict):
        """窗口到期处理：追踪@检查 → 成本控制 → LLM 生成 → 回复。同队列串行。"""
        if not q["msgs"]:
            return
        q["processing"] = True
        try:
            batch = list(q["msgs"])
            q["msgs"].clear()
            client_id, room, speaker = self._parse_queue_key(key)
            cfg = q["cfg"]
            mode = cfg.get("mode", "at_only")
            window = int(cfg.get("merge_window", 4))

            # 追踪@：非追踪成员的批次直接丢弃（零 LLM 调用）
            if mode == "track_at" and not self._is_tracked(room, speaker):
                logger.info(
                    f"  ⏭ 非追踪成员批次丢弃（track_at）: {room}/{speaker or '（昵称未解析）'} "
                    f"{len(batch)}条（room_type={q['room_type']}，@我后 {q['cfg'].get('track_duration', 180)}s 内才回复）"
                )
                return

            # 设备级成本控制：并发上限 + 60s 调用上限，超限批次回队等下个窗口
            if not self._try_acquire_llm_slot(client_id):
                logger.info(
                    f"  ⏸ 超设备限额，批次回队等下窗口: {room}/{speaker}（并发≤{self._get_limits(client_id)[0]}，60s≤{self._get_limits(client_id)[1]}次）"
                )
                q["msgs"].extend(batch)
                return
            trace: List[dict] = []
            try:
                await self._call_agent_and_reply(
                    client_id, room, speaker, q["room_type"], batch, cfg, trace
                )
                q["retries"] = 0  # 成功处理一轮，重置重试计数
            except Exception as e:
                logger.error(
                    f"  ❌ 自动回复处理异常 robot={client_id} room={room} speaker={speaker}: {e}"
                )
                # 回复由智能体通过设备指令发出：已经发过就不许重投，否则客户收到两条
                if device_send_attempted(trace):
                    logger.warning(
                        f"  ⛔ 本轮已通过设备指令发出消息，放弃重投以免重复发送: {room}/{speaker}"
                    )
                elif q.get("retries", 0) < 2:
                    q["retries"] = q.get("retries", 0) + 1
                    q["msgs"].extend(batch)
            finally:
                self._release_llm_slot(client_id)
        finally:
            q["processing"] = False
            if q["msgs"] and q["timer"] is None:
                self._restart_timer(key, q, int(q["cfg"].get("merge_window", 4)))

    def _is_tracked(self, room: str, speaker: str) -> bool:
        """track_at 模式：该 (room, speaker) 是否在追踪期内

        speaker 为空表示设备端没解析出群成员昵称（队列 key 里也就没有成员段）：
        此时回退到"房间级"追踪标记（由 _route_auto_reply 在 @命中时登记），
        否则 @了机器人也会因为拿不到昵称而被丢弃。
        """
        if not speaker:
            return self._track_state.get((room, ""), 0) > time.time()
        return self._track_state.get((room, speaker), 0) > time.time()

    def _get_limits(self, client_id: str) -> Tuple[int, int]:
        """取该设备限额（策略组配置，未绑定回退默认）"""
        hit = self._strategy_cache.get(client_id)
        strategy = hit[1] if hit else None
        if strategy is not None:
            return (
                int(getattr(strategy, "max_concurrent_llm", None) or _DEFAULT_LIMITS[0]),
                int(getattr(strategy, "max_calls_per_min", None) or _DEFAULT_LIMITS[1]),
            )
        return _DEFAULT_LIMITS

    def _try_acquire_llm_slot(self, client_id: str) -> bool:
        """占用一次设备 LLM 调用额度（并发数 + 60s 滑动窗口），成功返回 True"""
        max_concurrent, max_per_min = self._get_limits(client_id)
        now = time.time()
        times = self._llm_call_times.setdefault(client_id, [])
        times[:] = [t for t in times if now - t < 60]
        if len(times) >= max_per_min:
            return False
        if self._llm_active.get(client_id, 0) >= max_concurrent:
            return False
        self._llm_active[client_id] = self._llm_active.get(client_id, 0) + 1
        times.append(now)
        return True

    def _release_llm_slot(self, client_id: str):
        n = self._llm_active.get(client_id, 0)
        if n > 0:
            self._llm_active[client_id] = n - 1

    async def _call_agent_and_reply(
        self,
        client_id: str,
        room: str,
        speaker: str,
        room_type: int,
        batch: List[dict],
        cfg: dict,
        trace: Optional[List[dict]] = None,
    ):
        """一次批次提交：读取智能体 → 组装输入 → 推给智能体。
        回复由智能体自己通过设备指令（203）发出，服务器**不代发**它的文本输出；
        文本输出只用于日志与历史记录。智能体返回空 = 判定无需回复。"""
        is_group = room_type in (1, 3)
        robot = await self._get_robot_cached(client_id)
        from ..services.agent_service import agent_service as _agent_service

        agent = None
        try:
            if robot and robot.agent_id:
                agent = await _agent_service.get_agent(robot.agent_id)
        except Exception as e:
            logger.warning(f"读取设备智能体失败 {client_id}: {e}")
            agent = None

        if agent is None or not agent.enabled:
            logger.info(
                f"  ⏭ 未绑定可用智能体，不回复: {room}/{speaker}（请到「智能体管理」创建并绑定）"
            )
            return

        texts = [e["text"] for e in batch if not e.get("image")]
        images = [e["text"] for e in batch if e.get("image")]
        user_text = "\n".join(texts)

        if trace is None:
            trace = []
        reply_text = await _agent_service.generate_reply(
            agent=agent,
            robot_id=client_id,
            room_name=room,
            room_type=room_type,
            user_text=user_text,
            image_paths=images,
            speaker=speaker if is_group else None,
            self_name=self.get_self_name(client_id) if is_group else "",
            reply_policy="auto",
            # room_type 来自设备上报的 101 事件（还经过"外部群"纠偏）→ 可信，可下发给设备校验
            room_type_trusted=True,
            trace=trace,
        )

        # 留痕三态：已通过设备指令回复 / 模型判定不回 / 模型没照做（返回了文本却没发）
        # （未绑定 203 授权、设备离线时会在第三步暴露出来，见 tool_call_audit）
        if device_send_attempted(trace):
            logger.info(
                f"  🤖 智能体已通过设备指令回复「{(reply_text or '')[:50]}」→ {room}"
                + (f" @{speaker}" if is_group else "")
            )
        elif not reply_text:
            logger.info(f"  🤫 智能体判定无需回复（未调用发送指令）: {room}/{speaker}")
        else:
            logger.warning(
                f"  ⚠️ 智能体返回了文本但未调用发送指令(203)，未发送任何消息: "
                f"{room}/{speaker}（请检查该智能体是否已勾选 203 授权）: {reply_text[:50]}"
            )

    async def _handle_raw_confirm(self, data: dict, client_id: str):
        """Handle raw confirm (execution result) from device"""
        items = data.get("list") or []
        for item in items:
            err_code = item.get("errorCode")
            err_reason = item.get("errorReason", "")
            success_list = item.get("successList") or []
            fail_list = item.get("failList") or []
            raw_msg = item.get("rawMsg", "")
            now_str = datetime.now().strftime("%H:%M:%S")

            if err_code == 0:
                logger.info(f"  ✅ Exec success: {success_list} | {err_reason}")
                self._store(
                    client_id,
                    "last_result",
                    {
                        "time": now_str,
                        "status": "success",
                        "success_list": success_list,
                        "fail_list": fail_list,
                        "reason": err_reason,
                        "raw_msg": raw_msg,
                    },
                )
            else:
                logger.warning(f"  ❌ Exec failed (err={err_code}): {err_reason}")
                self._store(
                    client_id,
                    "last_result",
                    {
                        "time": now_str,
                        "status": "failed",
                        "error_code": err_code,
                        "reason": err_reason,
                        "success_list": success_list,
                        "fail_list": fail_list,
                        "raw_msg": raw_msg,
                    },
                )

            # 508「获取全部群」回执：success_list 携带群名，补写进 groups，
            # 供测试页「群列表」区块与 workflow「508 全部群」等待使用。
            if err_code == 0 and success_list:
                cmd_type = None
                if isinstance(raw_msg, str) and raw_msg.strip():
                    try:
                        cmd_type = json.loads(raw_msg).get("type")
                    except Exception:
                        cmd_type = None
                if cmd_type == 508:
                    self._store(
                        client_id,
                        "groups",
                        {
                            "time": now_str,
                            "type": "全部群",
                            "count": len(success_list),
                            "items": success_list,
                            "raw": item,
                        },
                    )

            # 507「获取全部好友」回执：设备端 getAllFriendInfo 只发回执、不发 507 数据
            # 消息，nameList（客户名单）在 rawMsg 里；补写进 contacts，供 workflow
            # 「507 全部好友」等待使用（否则 expect_type=507 等 contacts 会超时）。
            if err_code == 0:
                cmd_type = None
                if isinstance(raw_msg, str) and raw_msg.strip():
                    try:
                        cmd_type = json.loads(raw_msg).get("type")
                    except Exception:
                        cmd_type = None
                if cmd_type == 507:
                    try:
                        name_list = json.loads(raw_msg).get("nameList") or []
                    except Exception:
                        name_list = []
                    self._store(
                        client_id,
                        "contacts",
                        {
                            "time": now_str,
                            "type": "全部好友",
                            "count": len(name_list),
                            "items": name_list,
                            "raw": item,
                        },
                    )

    # ── Data storage helpers ───────────────────────────────

    def _store(self, client_id: str, key: str, value: dict, maxlen: int = 1, dedup_field: str = None):
        """Store received data for test page display

        dedup_field: 按该字段值去重合并——同值条目原位更新，不同值条目追加，
                     用于“全部群详情(501)”这类需要保留全部条目的逐条上报。
        """
        if client_id not in self.received_data:
            self.received_data[client_id] = {}
        if key not in self.received_data[client_id]:
            self.received_data[client_id][key] = []
        lst = self.received_data[client_id][key]
        dedup_value = value.get(dedup_field) if dedup_field else None
        if dedup_field and dedup_value is not None:
            for idx, existing in enumerate(lst):
                if existing.get(dedup_field) == dedup_value:
                    lst[idx] = value
                    break
            else:
                lst.append(value)
        else:
            lst.append(value)
        if len(lst) > maxlen:
            lst.pop(0)

    # ── 设备事件监听（任务模块条件触发用） ─────────────

    def add_event_listener(self, listener) -> None:
        """注册设备事件监听器，签名 listener(event_type: str, payload: dict)"""
        if listener not in self._event_listeners:
            self._event_listeners.append(listener)

    def _emit_event(self, event_type: str, **payload) -> None:
        """广播设备事件（同步、非阻塞；监听器自行决定是否异步处理）"""
        for fn in list(self._event_listeners):
            try:
                fn(event_type, payload)
            except Exception:
                logger.exception(f"   ⚠ 事件监听器异常: {event_type}")

    def get_received_data(self, client_id: str) -> dict:
        """Get all received data for a client"""
        return self.received_data.get(client_id, {})

    def get_all_received_data(self) -> dict:
        """Get received data for all clients"""
        return dict(self.received_data)

    def get_self_name(self, client_id: str) -> str:
        """设备登录账号在企业微信里的显示名（用于群聊 @ 判定）。

        唯一来源是 503「我的信息」的 myInfo.name（设备连接时不会主动上报）。
        拿不到就返回空串，调用方走宽松兜底提示（见 agent_service.generate_reply）。
        """
        items = (self.received_data.get(client_id) or {}).get("my_info") or []
        for it in reversed(items):
            name = str(((it or {}).get("info") or {}).get("name") or "").strip()
            if name:
                return name
        return ""

    # ── Send reply ─────────────────────────────────────────

    async def send_reply(
        self,
        client_id: str,
        to_name: str,
        reply_text: str,
        room_type: int = 1,
        at: str = None,
        at_list: List[str] = None,
    ):
        """Send reply message to Aivbot client.

        at/at_list：群聊回复时携带，设备端将其转为真 @ 提及（203 指令扩展字段）。
        """
        msg_id = (
            f"server_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
        )
        cmd_item = {
            "type": 203,
            "roomType": room_type,
            "titleList": [to_name],
            "receivedContent": reply_text,
            "textType": 1,
        }
        if at:
            cmd_item["at"] = at
        if at_list:
            cmd_item["atList"] = at_list
        command = {
            "socketType": 2,
            "messageId": msg_id,
            "encryptType": 0,
            "list": [cmd_item],
        }
        success = await self.send_message(client_id, command)
        if success:
            logger.info(f"✅ Reply sent: {to_name} -> {reply_text}")
        return success

    def get_stats(self) -> dict:
        """Get service statistics"""
        return {
            "active_connections": len(self.active_connections),
            "total_connections": self.connection_count,
            "message_count": self.message_count,
            "clients": list(self.active_connections.keys()),
            "timestamp": datetime.now().isoformat(),
        }


# Global service instance
aivbot_service = AivbotService()
