"""
智能体（Agent）管理与 LLM 对话服务

功能：
- 支持创建多个智能体，每个设备（robot）可绑定一个智能体
- 通过 OpenAI 兼容的 /chat/completions 接口对接 DeepSeek / 通义 / 智谱 / Kimi 等 LLM
- 会话管理（对话不过长）：
    * 按 (robot_id, room_name) 维护对话上下文（存 MySQL）
    * max_context_turns 控制保留轮数，超出后从最旧丢弃
    * idle_reset_minutes 空闲超时后自动开启新会话（避免无限积累）
    * 更换绑定智能体后自动重置会话
"""

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
from datetime import datetime
from io import BytesIO
from typing import Any, List, Optional, Tuple

import aiomysql
import httpx

from ..models.agent import Agent, AgentCreateRequest
from .group_service import group_service
from .knowledge_service import knowledge_service
from .memory_service import memory_service
from .table_service import table_service
from .skill_service import skill_service

logger = logging.getLogger(__name__)

# 智能体用途：普通对话智能体 / 组织配置智能体（后者全局唯一、不属任何组织、工具集独立）
PROVISIONER_KIND = "provisioner"

# LLM 请求超时（秒）。stream=False，所以 read timeout 实际就是整次生成的墙钟上限。
# 实测思考模式吞吐：deepseek-flash ≈245 tok/s、deepseek-v4-pro ≈95 tok/s，
# 即 max_tokens=8192 的最坏耗时约 86s（v4-pro）—— 超时必须跟着预算一起放大，
# 否则调大 max_tokens 只会从「正文为空」变成「读超时」。
# ponytail: 天花板在此；要彻底摆脱总时长限制就改流式（SSE），届时本超时可回到 60s。
LLM_TIMEOUT = 120

# ── 联网工具（Function Calling）配置 ──────────────────
TOOL_MAX_ROUNDS = 5           # LLM 连续发起工具调用的最大轮数（防止无限循环）
TOOL_TIMEOUT = 20             # 单次 http_get / http_post 请求超时（秒）
TOOL_RESPONSE_MAX_CHARS = 6000  # 工具结果回传给 LLM 的最大字符数（防上下文过大）
_TOOLS_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": (
                "访问一个 URL 链接，使用 HTTP GET 方法获取页面或接口内容。"
                "适用于查询天气、新闻、汇率、订单状态等只读访问。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "完整的 URL，如 https://example.com/api/query?keyword=xx",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_post",
            "description": (
                "访问一个 URL 链接，使用 HTTP POST 方法提交 JSON 数据并获取返回内容。"
                "适用于需要提交查询条件、参数或表单数据的接口。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "完整的 URL，如 https://example.com/api/query",
                    },
                    "data": {
                        "type": "object",
                        "description": "要提交的 JSON 数据，如 {\"keyword\": \"xxx\"}",
                    },
                    "headers": {
                        "type": "object",
                        "description": "可选的额外请求头，如 {\"Content-Type\": \"application/json\"}",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "table_query",
            "description": (
                "查询已授权的自定义表格数据（只读）。"
                "table 填表格标识 table_key（如 tbl_12，可从表格管理页获取）。"
                "where 填查询条件：简单等值可用 {\"字段名\": 值}，复杂条件用 "
                "[{\"field\":\"字段名\",\"op\":\"=\",\"value\":值}]，op 支持 =,!=,>,<,>=,<=,like。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "表格标识 table_key，如 tbl_12",
                    },
                    "where": {
                        "oneOf": [{"type": "object"}, {"type": "array"}],
                        "description": "查询条件",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回行数，默认 50",
                    },
                },
                "required": ["table"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "table_create",
            "description": (
                "创建一个新的自定义表格（表格管理页可见，名称需唯一）。"
                "name 为表格名称；fields 为字段定义数组，元素为 "
                '{"name":"字段名","type":"varchar|int|float|datetime|text|json","label":"中文名","required":true}。'
                "创建成功后该表格会自动授权给当前智能体（读写权限），可直接继续插入/查询数据。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "表格名称，如：客户名单"},
                    "description": {"type": "string", "description": "可选，表格用途描述"},
                    "fields": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "字段定义数组：[{name,type,label,required}]",
                    },
                },
                "required": ["name", "fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "table_insert",
            "description": "向已授权的自定义表格插入一行数据（需要写权限）。row 的键为表格字段名。",
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "表格标识 table_key，如 tbl_12"},
                    "row": {"type": "object", "description": "要插入的数据，键为字段名"},
                },
                "required": ["table", "row"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "table_update",
            "description": (
                "更新已授权表格中指定 id 的一行数据（需要写权限）。"
                "调用前应先 table_query 确认目标行存在。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "表格标识 table_key，如 tbl_12"},
                    "id": {"type": "integer", "description": "要更新的行 id"},
                    "fields": {"type": "object", "description": "要更新的字段值，键为字段名"},
                },
                "required": ["table", "id", "fields"],
            },
        },
    },
]

# 长期记忆写入工具：只在智能体选了「维护用的记忆集」时才加入工具列表（见 _build_tools）。
# 描述就是给模型的行为规范（模型决定要不要调用时只读这里），所以写得具体些。
_MEMORY_SAVE_TOOL = {
    "type": "function",
    "function": {
        "name": "memory_save",
        "description": (
            "把一条关于当前客户的长期事实记到记忆里（下次对话会自动带上这些记忆）。"
            "值得记：客户身份/公司/角色、偏好与禁忌、产品兴趣、订购意向、订单要点、"
            "承诺与约定时间、投诉与顾虑。"
            "不值得记：寒暄、一次性的临时问答、知识库里已有的通用信息、你自己的推测或主观评价。"
            "一条只记一个事实，写成一句话（≤200 字）；同一件事不要重复记。"
            "customer 填客户规范名：私聊填会话名；群聊填当前发言者名（群名不是客户）；"
            "若上下文的名册里已有这位客户，必须用名册里的规范名，不要另起新名字。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要记住的客户事实，一句话（≤200 字）",
                },
                "customer": {
                    "type": "string",
                    "description": (
                        "客户规范名。私聊填当前会话名；群聊填当前发言者名；"
                        "名册里已有该客户时必须用名册中的规范名。留空则按当前会话名推断。"
                    ),
                },
            },
            "required": ["content"],
        },
    },
}

# 项目根目录（app/services/agent_service.py → 上3级）。imagePath 是相对项目根的 uploads/... 路径。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 设备指令类型 → 中文说明（与 routes/workflows.py CMD_OPTIONS 保持一致）
_CMD_LABELS = {
    203: "发送消息",
    204: "指定回复(@)",
    205: "转发消息",
    206: "创建群",
    207: "修改群配置",
    213: "修改好友备注/按手机号添加好友",
    218: "推送文件",
    219: "解散群",
    220: "从外部群添加好友",
    221: "添加待办",
    222: "打卡",
    223: "切换企业",
    224: "推送链接",
    225: "修改群成员信息",
    226: "撤回消息",
    233: "扫一扫",
    234: "删除联系人",
    501: "获取群详情",
    502: "获取好友资料",
    503: "获取我的信息",
    505: "获取最近聊天",
    506: "获取企业列表",
    507: "获取全部好友",
    508: "获取全部群",
    509: "获取本地文件",
}

# 设备指令参数契约：required=必填字段；optional=可选字段；example=示例
# （与 routes/workflows.py CMD_PARAM_INFO 保持一致；智能体调用时按此传参并校验）
_CMD_PARAM_SCHEMA = {
    203: {
        "required": ["titleList", "receivedContent", "textType"],
        "optional": ["at", "atList"],
        "example": {"titleList": "文件传输助手", "receivedContent": "你好", "textType": "1"},
    },
    204: {
        "required": ["titleList", "receivedName", "originalContent", "textType", "receivedContent"],
        "optional": [],
        "example": {
            "titleList": "文件传输助手", "receivedName": "张三", "originalContent": "你好",
            "receivedContent": "收到", "textType": "1",
        },
    },
    205: {
        "required": ["titleList", "receivedName", "originalContent", "textType", "nameList"],
        "optional": ["extraText"],
        "example": {
            "titleList": "文件传输助手", "receivedName": "张三", "originalContent": "你好",
            "textType": "1", "nameList": "客户群", "extraText": "请查收",
        },
    },
    206: {
        "required": ["groupName", "selectList"],
        "optional": ["groupAnnouncement", "groupRemark", "groupTemplate"],
        "example": {"groupName": "项目群", "selectList": "张三,李四", "groupAnnouncement": "欢迎加入"},
    },
    207: {
        "required": ["groupName"],
        "optional": [
            "newGroupName", "newGroupAnnouncement", "selectList", "removeList",
            "groupRemark", "groupTemplate",
        ],
        "example": {"groupName": "项目群", "newGroupName": "项目群2", "removeList": "张三"},
    },
    213: {
        # 同指令号双用法：改备注(friend.name+markName) 或 加好友(friend.phone+备注/附言)；发送前自动包装进 friend 对象
        "required": [],
        "optional": [
            "friend_name", "mark_name", "friend_phone", "friend_markName",
            "friend_markCorp", "friend_markExtra", "friend_leavingMsg",
        ],
        "example": {"friend_name": "张三", "mark_name": "销售-张"},
    },
    218: {
        "required": ["titleList", "objectName"],
        "optional": ["fileUrl", "fileType", "extraText", "fileBase64"],
        "example": {"titleList": "文件传输助手", "objectName": "报价单.pdf", "fileUrl": "https://x.com/a.pdf"},
    },
    219: {
        "required": ["groupName"],
        "optional": [],
        "example": {"groupName": "项目群"},
    },
    220: {
        "required": ["groupName", "friend_name"],
        "optional": [],
        "example": {"groupName": "行业交流群", "friend_name": "张三"},
    },
    221: {
        "required": ["receivedContent"],
        "optional": ["titleList", "description"],
        "example": {"receivedContent": "下午开会", "titleList": "张三", "description": "3点"},
    },
    222: {"required": [], "optional": [], "example": {}},
    223: {
        "required": ["objectName"],
        "optional": [],
        "example": {"objectName": "某某科技有限公司"},
    },
    224: {
        "required": ["titleList", "originalContent"],
        "optional": ["objectName", "receivedContent", "fileUrl", "extraText"],
        "example": {
            "titleList": "文件传输助手", "originalContent": "https://example.com/article",
            "objectName": "文章标题",
        },
    },
    225: {
        "required": ["groupName", "friend_name"],
        "optional": ["friend_markName"],
        "example": {"groupName": "项目群", "friend_name": "张三", "friend_markName": "组长"},
    },
    226: {
        "required": ["titleList", "originalContent", "textType"],
        "optional": [],
        "example": {"titleList": "文件传输助手", "originalContent": "说错的话", "textType": "1"},
    },
    233: {
        "required": ["fileUrl"],
        "optional": [],
        "example": {"fileUrl": "https://x.com/qrcode.png"},
    },
    234: {
        "required": ["friend_name"],
        "optional": [],
        "example": {"friend_name": "张三"},
    },
    501: {
        "required": ["selectList"],
        "optional": [],
        "example": {"selectList": "项目群"},
    },
    502: {
        "required": ["friend_name"],
        "optional": [],
        "example": {"friend_name": "张三"},
    },
    503: {"required": [], "optional": [], "example": {}},
    505: {"required": [], "optional": [], "example": {}},
    506: {"required": [], "optional": [], "example": {}},
    507: {"required": [], "optional": [], "example": {}},
    508: {"required": [], "optional": [], "example": {}},
    509: {
        "required": ["fileUrl"],
        "optional": [],
        "example": {"fileUrl": "/storage/emulated/0/Download/a.pdf"},
    },
}

# run_workflow / device_command 工具被工作流内 LLM 调用时的递归防护:
# 工作流内的 llm 节点不再注入 run_workflow 工具,避免"工作流→LLM→工作流"死循环
# （通过 _chat_completion 的 allow_extra_tools=False 实现）。

# PaddleOCR 懒加载单例（首次用到图片OCR策略时才初始化，避免拖慢启动）
_ocr_engine = None


# @ 提及的两种写法：半角 @ 与全角 ＠（企微正文里两种都出现过）
_AT_CHARS = "@＠"


def mentions_me(text: str, self_name: str) -> bool:
    """正文里是否 @ 了本机（决定"要不要把这条消息交给模型"）。

    真机事实（2026-10-09 实测原始 101 事件）：设备上报的消息里**没有** at/atList，
    服务端只能从正文里认；而 503「我的信息」能给出本机在企业微信里的名字。

    判据取向：**名字怎么做匹配要精准，但"名字拿不到"时必须保守**（漏唤醒=客户 @ 了机器人它一声不吭）：
      · 名字未知 → 退回旧的"正文含 @"（保持现有行为，不退化）；
      · 名字已知 → **只认"@ 后面那串名字"与自己完全相等**（`@张三` 唤醒；`@张三-客服`、
        `@示例科技张三`、`@小助理`、`@李四` 都不唤醒）—— 精准优先，误唤醒一次就是一次多余的
        插话 + 一次 LLM 花费；
      · 正文里只有孤立的 `@`（手打没选中任何人）→ 保守唤醒。
    """
    text = text or ""
    if not self_name:
        return any(ch in text for ch in _AT_CHARS)
    tokens = re.findall(rf"[{_AT_CHARS}]([^\s{_AT_CHARS}]+)", text)
    if not tokens:
        # 没有可解析的名字（例如只打了一个 @）→ 保守唤醒，避免漏
        return any(ch in text for ch in _AT_CHARS)
    name = self_name.strip()
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if tok == name:
            return True
    return False


class AgentService:
    """智能体管理服务（MySQL 存储 + LLM 调用）"""

    def __init__(self):
        self._pool: Optional[aiomysql.Pool] = None

    def set_pool(self, pool: aiomysql.Pool):
        self._pool = pool

    # ── 建表 ────────────────────────────────────────────

    async def ensure_table(self):
        """创建 agents / agent_conversations 表，并为 robots 表补 agent_id 列"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agents (
                        id                 INT AUTO_INCREMENT PRIMARY KEY,
                        name               VARCHAR(100) NOT NULL,
                        description        VARCHAR(500) NOT NULL DEFAULT '',
                        provider           VARCHAR(30)  NOT NULL DEFAULT 'custom',
                        base_url           VARCHAR(255) NOT NULL DEFAULT '',
                        model              VARCHAR(100) NOT NULL DEFAULT '',
                        api_key            VARCHAR(255) NOT NULL DEFAULT '',
                        system_prompt      TEXT,
                        temperature        FLOAT        NOT NULL DEFAULT 0.7,
                        max_tokens         INT          NOT NULL DEFAULT 8192,
                        max_context_turns  INT          NOT NULL DEFAULT 20,
                        idle_reset_minutes INT          NOT NULL DEFAULT 30,
                        image_strategy     TINYINT      NOT NULL DEFAULT 0,
                        enable_tools       TINYINT      NOT NULL DEFAULT 0,
                        enable_http        TINYINT      NOT NULL DEFAULT 0,
                        allow_table_create TINYINT      NOT NULL DEFAULT 0,
                        allow_task_create  TINYINT      NOT NULL DEFAULT 0,
                        enabled            TINYINT      NOT NULL DEFAULT 1,
                        created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                # 老库幂等补列（列已存在时忽略）
                for col in (
                    "image_strategy TINYINT NOT NULL DEFAULT 0",
                    "enable_tools TINYINT NOT NULL DEFAULT 0",
                    "enable_http TINYINT NOT NULL DEFAULT 0",
                    "allow_table_create TINYINT NOT NULL DEFAULT 0",
                    "allow_task_create TINYINT NOT NULL DEFAULT 0",
                    "kind VARCHAR(20) NOT NULL DEFAULT 'chat'",
                ):
                    try:
                        await cur.execute(f"ALTER TABLE agents ADD COLUMN {col}")
                    except Exception:
                        pass  # 列已存在
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_conversations (
                        id          INT AUTO_INCREMENT PRIMARY KEY,
                        agent_id    INT          NOT NULL,
                        robot_id    VARCHAR(10)  NOT NULL,
                        room_name   VARCHAR(200) NOT NULL,
                        speaker     VARCHAR(200) NOT NULL DEFAULT '',
                        room_type   INT          NOT NULL DEFAULT 1,
                        history     TEXT,
                        last_msg_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_conv (robot_id, room_name, speaker)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                # agent_conversations 老库幂等补 speaker 列（列已存在时忽略）
                try:
                    await cur.execute(
                        "ALTER TABLE agent_conversations ADD COLUMN speaker VARCHAR(200) NOT NULL DEFAULT ''"
                    )
                except Exception:
                    pass  # 列已存在
                # 唯一键改为 (robot_id, room_name, speaker)：先删旧索引再建新索引（幂等）
                try:
                    await cur.execute("ALTER TABLE agent_conversations DROP INDEX uk_conv")
                except Exception:
                    pass  # 索引不存在
                try:
                    await cur.execute(
                        "ALTER TABLE agent_conversations ADD UNIQUE KEY uk_conv (robot_id, room_name, speaker)"
                    )
                except Exception:
                    pass  # 索引已存在
                # robots 表补 agent_id 列（幂等）
                try:
                    await cur.execute(
                        "ALTER TABLE robots ADD COLUMN agent_id INT DEFAULT NULL"
                    )
                except Exception:
                    pass  # 列已存在
                # 工具调用审计日志（安全追溯底线）
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tool_call_audit (
                        id BIGINT PRIMARY KEY AUTO_INCREMENT,
                        agent_id INT NOT NULL,
                        robot_id VARCHAR(50) NOT NULL DEFAULT '',
                        room_name VARCHAR(200) NOT NULL DEFAULT '',
                        requester_role VARCHAR(20) NOT NULL DEFAULT 'customer',
                        tool VARCHAR(100) NOT NULL,
                        args TEXT,
                        decision VARCHAR(20) NOT NULL DEFAULT 'allow',
                        reason VARCHAR(500) NOT NULL DEFAULT '',
                        result TEXT,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        KEY idx_audit_agent (agent_id, created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                # 智能体可调用工作流 / 设备指令授权表
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_workflows (
                        agent_id    INT NOT NULL,
                        workflow_id INT NOT NULL,
                        created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (agent_id, workflow_id),
                        KEY idx_aw_workflow (workflow_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_commands (
                        agent_id   INT NOT NULL,
                        cmd_type   INT NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (agent_id, cmd_type)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
                )
                await conn.commit()
        await self._cleanup_stale()
        logger.info("✅ agents & agent_conversations tables ensured")

    async def _cleanup_stale(self, days: int = 7):
        """清理超过 N 天未更新的会话记录，防止表无限膨胀"""
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM agent_conversations WHERE updated_at < NOW() - INTERVAL %s DAY",
                        (days,),
                    )
                    await conn.commit()
        except Exception as e:
            logger.warning(f"cleanup agent_conversations failed: {e}")

    # ── 行 → 模型 ───────────────────────────────────────

    def _row_to_agent(self, row) -> Agent:
        return Agent(
            id=row[0],
            name=row[1],
            description=row[2] or "",
            provider=row[3],
            base_url=row[4] or "",
            model=row[5] or "",
            api_key=row[6] or "",
            system_prompt=row[7] or "",
            temperature=row[8] if row[8] is not None else 0.7,
            max_tokens=row[9],
            max_context_turns=row[10],
            idle_reset_minutes=row[11],
            enabled=bool(row[12]),
            created_at=row[13].isoformat() if row[13] else "",
            robot_count=row[14] if len(row) > 14 and row[14] else 0,
            image_strategy=row[15] if len(row) > 15 and row[15] is not None else 0,
            enable_tools=bool(row[16]) if len(row) > 16 and row[16] is not None else False,
            allow_task_create=bool(row[17]) if len(row) > 17 and row[17] is not None else False,
            enable_http=bool(row[18]) if len(row) > 18 and row[18] is not None else False,
            allow_table_create=bool(row[19]) if len(row) > 19 and row[19] is not None else False,
            kind=(row[20] or "chat") if len(row) > 20 else "chat",
        )

    # ── CRUD ────────────────────────────────────────────

    async def list_agents(self, include_provisioner: bool = False) -> List[Agent]:
        """列出所有智能体（含绑定设备数）。

        默认不含「组织配置智能体」（kind=provisioner）——它是系统级工具，
        不属于任何组织，也不该出现在设备绑定/任务/工作流等下拉里。
        """
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT a.id,a.name,a.description,a.provider,a.base_url,a.model,a.api_key,
                              a.system_prompt,a.temperature,a.max_tokens,a.max_context_turns,
                              a.idle_reset_minutes,a.enabled,a.created_at,
                              (SELECT COUNT(*) FROM robots r WHERE r.agent_id = a.id) AS robot_count,
                              a.image_strategy, a.enable_tools, a.allow_task_create,
                              a.enable_http, a.allow_table_create, a.kind
                       FROM agents a
                       ORDER BY a.id ASC"""
                )
                rows = await cur.fetchall()
        agents = [self._row_to_agent(r) for r in rows]
        if include_provisioner:
            return agents
        return [a for a in agents if a.kind != PROVISIONER_KIND]

    async def get_agent(self, agent_id: int) -> Optional[Agent]:
        """获取单个智能体"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT a.id,a.name,a.description,a.provider,a.base_url,a.model,a.api_key,
                              a.system_prompt,a.temperature,a.max_tokens,a.max_context_turns,
                              a.idle_reset_minutes,a.enabled,a.created_at,
                              (SELECT COUNT(*) FROM robots r WHERE r.agent_id = a.id) AS robot_count,
                              a.image_strategy, a.enable_tools, a.allow_task_create,
                              a.enable_http, a.allow_table_create, a.kind
                       FROM agents a WHERE a.id=%s""",
                    (agent_id,),
                )
                row = await cur.fetchone()
        if row:
            return self._row_to_agent(row)
        return None

    # ── 组织配置智能体（kind=provisioner，全局唯一）────────────

    async def get_provisioner(self) -> Optional[Agent]:
        """取组织配置智能体（不属任何组织，工具集是 provision_* 系列）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT a.id,a.name,a.description,a.provider,a.base_url,a.model,a.api_key,
                              a.system_prompt,a.temperature,a.max_tokens,a.max_context_turns,
                              a.idle_reset_minutes,a.enabled,a.created_at,
                              0 AS robot_count,
                              a.image_strategy, a.enable_tools, a.allow_task_create,
                              a.enable_http, a.allow_table_create, a.kind
                       FROM agents a WHERE a.kind=%s ORDER BY a.id ASC LIMIT 1""",
                    (PROVISIONER_KIND,),
                )
                row = await cur.fetchone()
        return self._row_to_agent(row) if row else None

    async def ensure_provisioner(self, name: str, description: str, prompt: str) -> Agent:
        """确保组织配置智能体存在（不存在则创建），返回它。

        LLM 配置（provider/base_url/model/api_key）留空，由管理员在配置页填写。
        提示词只在创建时写入 —— 管理员改过之后永不覆盖（要恢复默认用配置页的按钮）。
        """
        agent = await self.get_provisioner()
        if agent:
            return agent
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM agents WHERE name=%s", (name,))
                if await cur.fetchone():
                    name = f"{name}（系统）"
                await cur.execute(
                    """INSERT INTO agents (name, description, provider, base_url, model, api_key,
                                           system_prompt, temperature, max_tokens, max_context_turns,
                                           idle_reset_minutes, image_strategy, enable_tools,
                                           enable_http, allow_table_create, allow_task_create,
                                           enabled, kind)
                       VALUES (%s,%s,'custom','','','',%s,0.7,8192,20,30,0,1,0,0,0,1,%s)""",
                    (name, description, prompt, PROVISIONER_KIND),
                )
                new_id = cur.lastrowid
                await conn.commit()
        logger.info(f"✅ 已创建组织配置智能体 #{new_id}「{name}」")
        agent = await self.get_provisioner()
        assert agent is not None
        return agent

    async def create_agent(self, req: AgentCreateRequest) -> int:
        """创建智能体，返回新 id"""
        self._validate(req)
        if not req.api_key.strip():
            raise ValueError("API Key 不能为空")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM agents WHERE name=%s", (req.name.strip(),)
                )
                if await cur.fetchone():
                    raise ValueError(f"智能体名称「{req.name}」已存在")
                await cur.execute(
                    """INSERT INTO agents (name, description, provider, base_url, model, api_key,
                                           system_prompt, temperature, max_tokens, max_context_turns,
                                           idle_reset_minutes, image_strategy, enable_tools,
                                           enable_http, allow_table_create, allow_task_create, enabled)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        req.name.strip(),
                        req.description.strip(),
                        req.provider,
                        req.base_url.strip().rstrip("/"),
                        req.model.strip(),
                        req.api_key.strip(),
                        req.system_prompt,
                        float(req.temperature),
                        int(req.max_tokens),
                        int(req.max_context_turns),
                        int(req.idle_reset_minutes),
                        int(req.image_strategy),
                        1 if req.enable_tools else 0,
                        1 if req.enable_http else 0,
                        1 if req.allow_table_create else 0,
                        1 if req.allow_task_create else 0,
                        1 if req.enabled else 0,
                    ),
                )
                new_id = cur.lastrowid
                await conn.commit()
        logger.info(f"✅ Created agent: {req.name} (provider={req.provider}, model={req.model})")
        return new_id

    async def update_agent(self, agent_id: int, req: AgentCreateRequest) -> bool:
        """更新智能体。api_key 传空串时保留原 Key。"""
        self._validate(req)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT api_key FROM agents WHERE id=%s", (agent_id,))
                row = await cur.fetchone()
                if row is None:
                    return False
                await cur.execute(
                    "SELECT id FROM agents WHERE name=%s AND id<>%s",
                    (req.name.strip(), agent_id),
                )
                if await cur.fetchone():
                    raise ValueError(f"智能体名称「{req.name}」已被其他智能体使用")
                api_key = req.api_key.strip() or (row[0] or "")
                await cur.execute(
                    """UPDATE agents SET name=%s, description=%s, provider=%s, base_url=%s, model=%s,
                                         api_key=%s, system_prompt=%s, temperature=%s, max_tokens=%s,
                                         max_context_turns=%s, idle_reset_minutes=%s,
                                         image_strategy=%s, enable_tools=%s, enable_http=%s,
                                         allow_table_create=%s, allow_task_create=%s,
                                         enabled=%s
                       WHERE id=%s""",
                    (
                        req.name.strip(),
                        req.description.strip(),
                        req.provider,
                        req.base_url.strip().rstrip("/"),
                        req.model.strip(),
                        api_key,
                        req.system_prompt,
                        float(req.temperature),
                        int(req.max_tokens),
                        int(req.max_context_turns),
                        int(req.idle_reset_minutes),
                        int(req.image_strategy),
                        1 if req.enable_tools else 0,
                        1 if req.enable_http else 0,
                        1 if req.allow_table_create else 0,
                        1 if req.allow_task_create else 0,
                        1 if req.enabled else 0,
                        agent_id,
                    ),
                )
                await conn.commit()
                # 注意：不能以 rowcount 判断成败——字段值与原值相同时
                # MySQL 返回受影响行数 0，但更新实际成功（如仅勾选知识库保存）。
                # agent 存在性已在函数开头检查，UPDATE 执行成功即视为成功。
        # 修改对话参数后，历史会话继续沿用（由 generate_reply 按新参数裁剪）
        logger.info(f"✅ Updated agent #{agent_id}: {req.name}")
        return True

    async def update_agent_prompt(self, agent_id: int, system_prompt: str) -> bool:
        """只改系统提示词（不动 LLM 配置）。

        给「恢复默认提示词」用：配置智能体可能还没填 base_url/model，
        走 update_agent 整表回写会被 _validate 拦下。
        """
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM agents WHERE id=%s", (agent_id,))
                if not await cur.fetchone():
                    return False
                await cur.execute(
                    "UPDATE agents SET system_prompt=%s WHERE id=%s",
                    (system_prompt, agent_id),
                )
                await conn.commit()
        return True

    async def delete_agent(self, agent_id: int) -> bool:
        """删除智能体：解绑所有设备、删除其会话记录"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM agents WHERE id=%s", (agent_id,))
                if not await cur.fetchone():
                    return False
                await cur.execute(
                    "UPDATE robots SET agent_id=NULL WHERE agent_id=%s", (agent_id,)
                )
                await cur.execute(
                    "DELETE FROM agent_conversations WHERE agent_id=%s", (agent_id,)
                )
                await cur.execute(
                    "DELETE FROM agent_knowledge WHERE agent_id=%s", (agent_id,)
                )
                await cur.execute(
                    "DELETE FROM agent_group_members WHERE agent_id=%s", (agent_id,)
                )
                await cur.execute(
                    "DELETE FROM org_group_members "
                    "WHERE member_type='agent' AND member_id=%s",
                    (agent_id,),
                )
                await cur.execute(
                    "DELETE FROM agent_workflows WHERE agent_id=%s", (agent_id,)
                )
                await cur.execute(
                    "DELETE FROM agent_commands WHERE agent_id=%s", (agent_id,)
                )
                await cur.execute("DELETE FROM agents WHERE id=%s", (agent_id,))
                await conn.commit()
        logger.info(f"🗑 Deleted agent #{agent_id} and its conversations")
        return True

    async def clear_test_conversations(self, agent_id: int) -> int:
        """清空该智能体的测试会话（合成 robot_id 或「智能体测试」房间），
        避免测试历史污染导致 LLM 复述旧回答而不调用工具。"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM agent_conversations "
                    "WHERE agent_id=%s AND (robot_id LIKE 'test%%' OR room_name='智能体测试')",
                    (agent_id,),
                )
                await conn.commit()
                return cur.rowcount

    # ── 智能体授权:可调用工作流 / 设备指令 ─────────────

    async def set_agent_workflows(self, agent_id: int, workflow_ids: List[int]):
        """全量重设智能体可调用的工作流 id 列表"""
        wids = [int(x) for x in (workflow_ids or []) if str(x).strip()]
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM agent_workflows WHERE agent_id=%s", (agent_id,)
                )
                for wid in wids:
                    await cur.execute(
                        "INSERT IGNORE INTO agent_workflows (agent_id, workflow_id) VALUES (%s,%s)",
                        (agent_id, wid),
                    )
                await conn.commit()

    async def get_agent_workflows(self, agent_id: int) -> List[int]:
        """返回智能体可调用的工作流 id 列表"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT workflow_id FROM agent_workflows WHERE agent_id=%s "
                    "ORDER BY workflow_id ASC",
                    (agent_id,),
                )
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def set_agent_commands(self, agent_id: int, cmd_types: List[int]):
        """全量重设智能体可调用的设备指令类型列表"""
        cmds = [int(x) for x in (cmd_types or []) if str(x).strip()]
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM agent_commands WHERE agent_id=%s", (agent_id,)
                )
                for cmd in cmds:
                    await cur.execute(
                        "INSERT IGNORE INTO agent_commands (agent_id, cmd_type) VALUES (%s,%s)",
                        (agent_id, cmd),
                    )
                await conn.commit()

    async def get_agent_commands(self, agent_id: int) -> List[int]:
        """返回智能体可调用的设备指令类型列表"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT cmd_type FROM agent_commands WHERE agent_id=%s "
                    "ORDER BY cmd_type ASC",
                    (agent_id,),
                )
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    def _validate(self, req: AgentCreateRequest):
        """通用字段校验"""
        if not req.name.strip():
            raise ValueError("智能体名称不能为空")
        if not req.base_url.strip():
            raise ValueError("API 基础地址（base_url）不能为空，如 https://api.deepseek.com/v1")
        if not req.model.strip():
            raise ValueError("模型名称（model）不能为空")
        if int(req.max_tokens) < 1:
            raise ValueError("max_tokens 必须 ≥ 1")
        if int(req.max_context_turns) < 1:
            raise ValueError("上下文保留轮数必须 ≥ 1")

    # ── 设备绑定 ────────────────────────────────────────

    async def get_robot_agent(self, robot_id: str) -> Optional[Agent]:
        """获取设备绑定的智能体"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT a.id,a.name,a.description,a.provider,a.base_url,a.model,a.api_key,
                              a.system_prompt,a.temperature,a.max_tokens,a.max_context_turns,
                              a.idle_reset_minutes,a.enabled,a.created_at,0,a.image_strategy,
                              a.enable_tools, a.allow_task_create,
                              a.enable_http, a.allow_table_create
                       FROM robots r JOIN agents a ON r.agent_id = a.id
                       WHERE r.robot_id=%s""",
                    (robot_id,),
                )
                row = await cur.fetchone()
        if row:
            return self._row_to_agent(row)
        return None

    async def bind_agent(self, robot_id: str, agent_id: int) -> bool:
        """绑定设备到智能体。约束：一个智能体只能关联一个设备。"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM agents WHERE id=%s", (agent_id,))
                if not await cur.fetchone():
                    return False
                # 一对一约束：该智能体已被其他设备绑定时拒绝，防止一个智能体服务多台设备
                await cur.execute(
                    "SELECT robot_id FROM robots WHERE agent_id=%s AND robot_id<>%s LIMIT 1",
                    (agent_id, robot_id),
                )
                other = await cur.fetchone()
                if other:
                    raise ValueError(
                        f"智能体 #{agent_id} 已被设备 {other[0]} 绑定，一个智能体只能关联一个设备，请先解绑旧设备"
                    )
                await cur.execute(
                    "UPDATE robots SET agent_id=%s WHERE robot_id=%s",
                    (agent_id, robot_id),
                )
                await conn.commit()
                affected = cur.rowcount
        if affected:
            logger.info(f"✅ Bound robot {robot_id} to agent #{agent_id}")
            return True
        return False

    async def unbind_agent(self, robot_id: str) -> bool:
        """解绑设备（agent_id 置空）"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE robots SET agent_id=NULL WHERE robot_id=%s", (robot_id,)
                )
                await conn.commit()
                affected = cur.rowcount
        if affected:
            logger.info(f"ℹ️ Unbound robot {robot_id} from agent")
            return True
        return False

    # ── LLM 调用 ────────────────────────────────────────

    # ── 工具定义动态构建 ─────────────────────────────

    async def _build_tools(self, agent: Agent, robot_id: str = "") -> List[dict]:
        """
        按智能体授权动态构建 Function Calling 工具定义：
        - 基础工具（http_get / http_post / table_create / table_query / table_insert / table_update）始终提供
        - 授权了工作流 → 追加 run_workflow（参数 workflow_id + input）
        - 授权了设备指令 → 追加 device_command（参数 cmd_type + params）
        """
        tools = [dict(t) for t in _TOOLS_DEFINITIONS]
        # 组织配置智能体：只给「创建组织及其资源」的 provision_* 工具，不给任何设备/工作流能力
        if getattr(agent, "kind", "chat") == PROVISIONER_KIND:
            from . import provision_service

            return [dict(t) for t in provision_service.TOOL_DEFINITIONS]
        # 按授权细化基础工具（最小权限）：
        # - 联网工具（http_get/http_post）需 enable_http
        # - 表格读写（query/insert/update）需存在表授权（无删除能力，工具集本无 table_delete）
        # - 建表（table_create）需 allow_table_create，与表读写授权相互独立
        if not getattr(agent, "enable_http", False):
            tools = [
                t for t in tools
                if t["function"]["name"] not in ("http_get", "http_post")
            ]
        has_table_grant = bool(await table_service.get_tables_of_agent(agent.id))
        if not has_table_grant:
            tools = [
                t for t in tools
                if t["function"]["name"] not in ("table_query", "table_insert", "table_update")
            ]
        if not getattr(agent, "allow_table_create", False):
            tools = [
                t for t in tools if t["function"]["name"] != "table_create"
            ]
        # 长期记忆写入：这台设备有「维护用的记忆集」才给 memory_save
        # （没绑设备/没设维护集 = 只读或不用记忆，不该有写工具）
        try:
            mem_link = await memory_service.get_robot_memory_sets(robot_id) if robot_id else {}
            if mem_link.get("write"):
                tools.append(dict(_MEMORY_SAVE_TOOL))
        except Exception as e:
            logger.warning(f"读取设备记忆集失败 robot={robot_id}: {e}")
        try:
            wf_ids = await self.get_agent_workflows(agent.id)
            if wf_ids:
                from .workflow_service import workflow_service as _wf_svc

                all_wf = await _wf_svc.list_workflows()
                wf_map = {w.id: w.name for w in all_wf}
                wf_schema = {}
                for w in all_wf:
                    if w.input_schema:
                        try:
                            wf_schema[w.id] = json.loads(w.input_schema)
                        except Exception:
                            wf_schema[w.id] = []
                # 为每个工作流生成独立的 run_workflow_{id} 工具，参数严格按声明的输入契约
                for wid in wf_ids:
                    schema = wf_schema.get(wid) or []
                    props = {}
                    required = []
                    for s in schema:
                        fname = str(s.get("name") or "").strip()
                        if not fname:
                            continue
                        ftype = str(s.get("type") or "string")
                        fdesc = str(s.get("description") or "")
                        props[fname] = {
                            "type": "string" if ftype == "number" else "string",
                            "description": fdesc or (f"工作流输入参数 {fname}"),
                        }
                        if s.get("required"):
                            required.append(fname)
                    schema_desc = self._describe_schema(schema)
                    tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": f"run_workflow_{wid}",
                                "description": (
                                    f"运行工作流「{wf_map.get(wid, wid)}」(#{wid})。"
                                    + (
                                        f" 输入参数：{schema_desc}。"
                                        if schema_desc
                                        else " 该工作流无需额外输入参数。"
                                    )
                                    + " user_text 会自动填入当前用户消息，无需手动传入；"
                                      "仅当声明了其他参数时才需要提供。执行后返回工作流结果。"
                                ),
                                "parameters": {
                                    "type": "object",
                                    "properties": props,
                                    "required": required,
                                },
                            },
                        }
                    )
            cmd_ids = await self.get_agent_commands(agent.id)
            if cmd_ids:
                for c in cmd_ids:
                    schema = _CMD_PARAM_SCHEMA.get(c, {"required": [], "optional": [], "example": {}})
                    props = {}
                    required = []
                    for fname in schema.get("required") or []:
                        props[fname] = {
                            "type": "string",
                            "description": f"必填参数 {fname}（{self._cmd_param_hint(c, fname)}）",
                        }
                        required.append(fname)
                    for fname in schema.get("optional") or []:
                        props[fname] = {
                            "type": "string",
                            "description": f"可选参数 {fname}",
                        }
                    example = schema.get("example") or {}
                    example_text = json.dumps(example, ensure_ascii=False) if example else "{}"
                    tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": f"device_command_{c}",
                                "description": (
                                    f"向当前会话绑定的设备下发企业微信指令：{_CMD_LABELS.get(c, c)}(#{c})。"
                                    f" 参数示例: {example_text}。"
                                    + (
                                        " 在会话中回复消息就用本指令（titleList 填当前会话名）。"
                                        if c == 203
                                        else ""
                                    )
                                    + (
                                        f" 必填参数: {', '.join(required)}。"
                                        if required
                                        else " 该指令无需必填参数。"
                                    )
                                ),
                                "parameters": {
                                    "type": "object",
                                    "properties": props,
                                    "required": required,
                                },
                            },
                        }
                    )
            if getattr(agent, "allow_task_create", False):
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": "task_create",
                            "description": (
                                "创建一个定时/条件触发任务（需智能体已获授权「允许创建任务」）。"
                                "trigger_type 填 schedule（定时：trigger_spec 填 "
                                '{"cron":"0 9 * * *"} 或 {"interval_sec":3600}；'
                                '单次执行可填 {"once_at":时间戳}，或顶层 once_at 传 '
                                '"YYYY-MM-DDTHH:MM"；重复次数用顶层 repeat_count，执行满 N 次后自动结束）'
                                "或 condition（条件：trigger_spec 填 "
                                '{"type":"friend_accepted","target":{"phone":"138..."}} '
                                '或 {"type":"message_from","target":"对方昵称"}，条件任务需提供 robot_id）。'
                                "action_type 填 workflow（执行工作流：action_spec 填 "
                                '{"workflow_id":3,"params":{}}）'
                                "或 agent（注入智能体指令：action_spec 填 "
                                '{"agent_id":2,"instruction":"..."}）。创建成功返回任务 id。'
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "description": "任务名"},
                                    "trigger_type": {
                                        "type": "string",
                                        "enum": ["schedule", "condition"],
                                        "description": "触发类型：schedule 定时 / condition 条件",
                                    },
                                    "trigger_spec": {
                                        "type": "object",
                                        "description": "触发配置：定时填 cron/interval_sec/once_at；条件填 type/target",
                                    },
                                    "once_at": {
                                        "type": "string",
                                        "description": "单次定时：具体执行时间（ISO 格式如 2026-08-20T09:00 或秒级时间戳），只执行一次",
                                    },
                                    "repeat_count": {
                                        "type": "integer",
                                        "description": "重复次数：重复任务执行满 N 次后自动结束（0=无限，默认 0）",
                                    },
                                    "action_type": {
                                        "type": "string",
                                        "enum": ["workflow", "agent"],
                                        "description": "动作类型：workflow 执行工作流 / agent 注入智能体指令",
                                    },
                                    "action_spec": {
                                        "type": "object",
                                        "description": "动作配置：workflow 填 workflow_id/params；agent 填 agent_id/instruction",
                                    },
                                    "robot_id": {
                                        "type": "string",
                                        "description": "设备 ID：任务绑定的执行设备（工作流设备指令节点等待该设备回报；agent 动作注入指令也以它为设备上下文）。不填时系统自动使用当前会话设备；condition 任务必填",
                                    },
                                    "org_id": {
                                        "type": "integer",
                                        "description": "组织 ID：任务归属的组织。不填时系统自动归属当前组织，否则归属智能体所属组织",
                                    },
                                    "enabled": {
                                        "type": "boolean",
                                        "description": "是否启用，默认 true",
                                    },
                                },
                                "required": [
                                    "name", "trigger_type", "trigger_spec",
                                    "action_type", "action_spec",
                                ],
                            },
                        },
                    }
                )
        except Exception as e:
            logger.warning(f"构建智能体工具失败 agent=#{agent.id}: {e}")
        return tools

    async def _chat_completion(
        self,
        agent: Agent,
        messages: List[dict],
        robot_id: str = "",
        allow_extra_tools: bool = True,
        tool_ctx: Optional[dict] = None,
        simulate: bool = False,
        trace: Optional[list] = None,
    ) -> str:
        """
        调用 OpenAI 兼容的 /chat/completions 接口，返回回复文本。

        若 agent.enable_tools 开启（联网工具），启用 Function Calling：
        - 请求附带动态工具定义（基础联网/表格工具 + 授权的工作流/设备指令工具）
        - LLM 可自行决定是否调用工具
        - 服务器执行真实调用，把结果以 tool 角色回传，循环至 LLM 给出最终回复
        - 最多循环 TOOL_MAX_ROUNDS 轮，超限报错防止死循环

        robot_id：当前会话绑定的设备 id，供 device_command / run_workflow 工具执行时使用。
        tool_ctx：当前会话上下文 dict（room_name / room_type / user_text / sender 等），
                  智能体触发 run_workflow 时自动注入为工作流的 input 上下文，
                  保证 {{input.user_text}} 等模板变量有值，不依赖 LLM 手动填写。
        allow_extra_tools=False：不注入工作流/设备指令工具（工作流内 LLM 节点用，防递归）。
        simulate：测试模式。为 True 时 run_workflow 内的设备指令节点不依赖真实设备
                  （返回模拟数据），device_command 工具只校验不真实下发。
        trace：可选的列表，非 None 时把每次工具调用的 (工具名, 参数, 结果) 追加进去，
               供「智能体测试运行」页面展示 LLM 的工具调用轨迹。
        """
        url = f"{agent.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {agent.api_key}",
            "Content-Type": "application/json",
        }
        # 复制消息列表，Function Calling 时会在其末尾追加 assistant/tool 消息，不污染调用方
        msgs = list(messages)
        payload = {
            "model": agent.model,
            "messages": msgs,
            "temperature": agent.temperature,
            "max_tokens": agent.max_tokens,
            "stream": False,
        }
        if agent.enable_tools:
            if allow_extra_tools:
                payload["tools"] = await self._build_tools(agent, robot_id)
            else:
                payload["tools"] = [dict(t) for t in _TOOLS_DEFINITIONS]
            payload.setdefault("tool_choice", "auto")

        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            for _round in range(TOOL_MAX_ROUNDS + 1):
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                try:
                    message = data["choices"][0]["message"]
                except (KeyError, IndexError, TypeError) as e:
                    raise RuntimeError(f"LLM 返回格式异常: {data}") from e

                tool_calls = message.get("tool_calls")
                if not tool_calls:
                    content = (message.get("content") or "").strip()
                    if not content:
                        raise RuntimeError(f"LLM 返回空内容: {data}")
                    return content

                # ── 执行工具调用，把结果回传给 LLM ──
                logger.info(
                    f"Agent #{agent.id} 触发工具调用，共 {len(tool_calls)} 个"
                )
                payload["messages"].append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                )
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or ""
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                        if not isinstance(args, dict):
                            args = {}
                    except Exception:
                        args = {}
                    result_text = await self._execute_tool(
                        name,
                        args,
                        agent.id,
                        robot_id=robot_id,
                        tool_ctx=tool_ctx,
                        simulate=simulate,
                    )
                    if trace is not None:
                        trace.append(
                            {
                                "tool": name,
                                "args": args,
                                "result": result_text,
                            }
                        )
                    payload["messages"].append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id") or "",
                            "content": result_text,
                        }
                    )
            raise RuntimeError(
                f"Function Calling 超过 {TOOL_MAX_ROUNDS} 轮仍未得到最终回复，请检查工具或提示词"
            )

    async def _execute_tool(
        self,
        name: str,
        args: dict,
        agent_id: int = 0,
        robot_id: str = "",
        tool_ctx: Optional[dict] = None,
        simulate: bool = False,
    ) -> str:
        """
        执行 LLM 请求的工具调用（唯一入口），返回给 LLM 的文本结果，并全量落审计日志。
        - 联网工具：仅允许 http/https 协议；结果截断到 TOOL_RESPONSE_MAX_CHARS
        - 表格工具：按 agent 授权校验（读/写），写操作必须有写权限
        - run_workflow：按授权校验后运行工作流（自动注入会话上下文）
        - device_command：按授权校验后向当前设备下发指令
        simulate=True 时（测试模式）：run_workflow 的设备指令节点用虚拟数据、
        device_command 只校验不真实下发。
        """
        result = await self._execute_tool_impl(
            name, args, agent_id, robot_id, tool_ctx, simulate
        )
        try:
            await self._log_tool_audit(
                name, args, agent_id, robot_id, tool_ctx, result
            )
        except Exception as e:
            logger.warning(f"工具调用审计日志写入失败: {e}")
        return result

    async def _execute_tool_impl(
        self,
        name: str,
        args: dict,
        agent_id: int = 0,
        robot_id: str = "",
        tool_ctx: Optional[dict] = None,
        simulate: bool = False,
    ) -> str:
        # 组织配置工具：只有 kind=provisioner 的智能体拿得到这些工具定义，
        # 这里再按 agent_id 复核一次身份，防止别的智能体伪造工具名调用
        if name.startswith("provision_"):
            from . import provision_service

            return await provision_service.execute(name, args, agent_id)
        # 工作流工具：运行授权工作流（工具名 run_workflow_{workflow_id}）
        if name.startswith("run_workflow_"):
            try:
                wf_id = int(name.split("_", 2)[2])
            except (IndexError, ValueError):
                wf_id = 0
            if wf_id > 0:
                args = dict(args)
                args["workflow_id"] = wf_id
                return await self._execute_workflow_tool(
                    args,
                    agent_id,
                    robot_id,
                    tool_ctx=tool_ctx,
                    simulate=simulate,
                )
            return f"错误: 无法解析工作流工具名「{name}」"
        # 设备指令工具：向当前设备下发指令（工具名 device_command_{cmd_type}）
        if name.startswith("device_command_"):
            try:
                cmd = int(name.split("_", 2)[2])
            except (IndexError, ValueError):
                cmd = 0
            if cmd > 0:
                args = dict(args)
                args["cmd_type"] = cmd
                return await self._execute_device_command_tool(
                    args, agent_id, robot_id, simulate=simulate, tool_ctx=tool_ctx
                )
            return f"错误: 无法解析设备指令工具名「{name}」"
        # 设备指令工具：向当前设备下发指令
        if name == "device_command":
            return await self._execute_device_command_tool(
                args, agent_id, robot_id, simulate=simulate, tool_ctx=tool_ctx
            )
        # 表格工具
        if name in ("table_query", "table_insert", "table_update", "table_create"):
            return await self._execute_table_tool(name, args, agent_id)
        # 长期记忆：写到当前设备的「维护集」；群聊里客户是发言者本人，私聊才是会话名
        if name == "memory_save":
            ctx = tool_ctx or {}
            is_group = int(ctx.get("room_type") or 0) in (1, 3)
            clue = ctx.get("speaker") if is_group else ctx.get("room_name")
            return await memory_service.save_memory(
                robot_id,
                str(args.get("customer") or ""),
                str(args.get("content") or ""),
                origin=str(ctx.get("room_name") or ""),
                clue=str(clue or ""),
            )
        # 创建任务工具（需智能体授权 allow_task_create）；
        # 透传当前会话设备 robot_id 与会话上下文 tool_ctx（含 org_id），
        # 供 LLM 未显式填写 robot_id / org_id 时自动继承
        if name == "task_create":
            return await self._execute_task_tool(
                args, agent_id, robot_id=robot_id, tool_ctx=tool_ctx
            )

        url = str(args.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return f"错误: URL 必须以 http:// 或 https:// 开头（收到：{url}）"
        headers = {"User-Agent": "Mozilla/5.0 (Aivbot Agent)"}
        extra_headers = args.get("headers")
        if isinstance(extra_headers, dict):
            headers.update({str(k): str(v) for k, v in extra_headers.items()})
        timeout = httpx.Timeout(TOOL_TIMEOUT)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                if name == "http_get":
                    resp = await client.get(url, headers=headers)
                elif name == "http_post":
                    data = args.get("data")
                    resp = await client.post(
                        url,
                        headers=headers,
                        json=data if isinstance(data, (dict, list)) else {},
                    )
                else:
                    return f"错误: 未知工具「{name}」"
                text = resp.text or ""
                if len(text) > TOOL_RESPONSE_MAX_CHARS:
                    text = text[:TOOL_RESPONSE_MAX_CHARS] + "\n…[内容过长已截断]"
                return f"HTTP {resp.status_code}\n{text}"
        except Exception as e:
            return f"请求失败: {e}"

    @staticmethod
    def _derive_requester_role(tool_ctx: Optional[dict]) -> str:
        """从会话上下文推导请求者角色（用于审计）"""
        if not tool_ctx:
            return "system"
        room = str(tool_ctx.get("room_name") or "")
        if room == "智能体测试":
            return "test"
        # 企微会话消息（单聊/群聊）统一视为客户来源；管理侧/内部触发暂无法区分
        return "customer"

    async def _log_tool_audit(
        self,
        name: str,
        args: dict,
        agent_id: int,
        robot_id: str,
        tool_ctx: Optional[dict],
        result: str,
    ) -> None:
        """工具调用全量审计落库（失败不阻塞主流程，由调用方捕获）"""
        role = self._derive_requester_role(tool_ctx)
        room = str((tool_ctx or {}).get("room_name") or "")
        try:
            args_json = json.dumps(args, ensure_ascii=False, default=str)[:2000]
        except Exception:
            args_json = str(args)[:2000]
        result_trunc = (result or "")[:2000]
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO tool_call_audit
                       (agent_id, robot_id, room_name, requester_role, tool, args, decision, reason, result)
                       VALUES (%s,%s,%s,%s,%s,%s,'allow','',%s)""",
                    (agent_id, robot_id, room, role, name, args_json, result_trunc),
                )
                await conn.commit()

    async def list_audit_logs(self, limit: int = 50, agent_id: int = 0) -> List[dict]:
        """最近的工具调用审计日志（倒序）"""
        sql = (
            "SELECT id, agent_id, robot_id, room_name, requester_role, tool, args, "
            "decision, reason, result, created_at FROM tool_call_audit"
        )
        conds, params = [], []
        if agent_id:
            conds.append("agent_id=%s")
            params.append(agent_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT %s"
        params.append(int(limit))
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, tuple(params))
                rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "agent_id": r[1],
                "robot_id": r[2],
                "room_name": r[3],
                "requester_role": r[4],
                "tool": r[5],
                "args": r[6],
                "decision": r[7],
                "reason": r[8],
                "result": r[9],
                "created_at": r[10].isoformat() if r[10] else "",
            }
            for r in rows
        ]

    async def _execute_workflow_tool(
        self,
        args: dict,
        agent_id: int,
        robot_id: str,
        tool_ctx: Optional[dict] = None,
        simulate: bool = False,
    ) -> str:
        """执行工作流工具：校验授权 → 校验必填参数 → 注入会话上下文 → 运行工作流 → 返回结果"""
        workflow_id = int(args.get("workflow_id") or 0)
        if workflow_id <= 0:
            return "错误: 缺少有效的 workflow_id"
        allowed = set(await self.get_agent_workflows(agent_id))
        if workflow_id not in allowed:
            return f"错误: 智能体 #{agent_id} 未获授权运行工作流 {workflow_id}（请到「智能体管理」勾选）"
        try:
            from .workflow_service import workflow_service as _wf_svc
            from .workflow_engine import workflow_engine

            # 执行身份 = 调用该工作流的智能体（LLM/表格节点使用其能力与授权）
            agent = await self.get_agent(agent_id)

            # 读取工作流输入参数声明,校验必填项
            schema = []
            wf_info = await _wf_svc.get_workflow(workflow_id)
            if wf_info and wf_info.get("input_schema"):
                try:
                    schema = json.loads(wf_info["input_schema"]) or []
                except Exception:
                    schema = []

            # 输入上下文 = 服务器自动注入的当前会话上下文 + LLM 显式传入的参数
            input_ctx = dict(tool_ctx or {})
            llm_input = args.get("input")
            if isinstance(llm_input, dict):
                input_ctx.update(llm_input)
            # LLM 按声明的字段直接传入（工具参数顶层即字段名）
            for s in schema:
                fname = str(s.get("name") or "").strip()
                if fname and fname in args:
                    input_ctx[fname] = args[fname]
            if robot_id:
                input_ctx.setdefault("robot_id", robot_id)

            # 必填校验：基于最终 input_ctx（user_text 由服务器自动注入,不算缺失）
            missing = [
                str(s.get("name"))
                for s in schema
                if s.get("required")
                and not str(input_ctx.get(str(s.get("name"))) or "").strip()
            ]
            if missing:
                return (
                    f"错误: 工作流 #{workflow_id} 缺少必填参数: {', '.join(missing)}。"
                    f"请补充后再调用。可用参数: {self._describe_schema(schema)}"
                )

            result = await workflow_engine.execute(
                workflow_id,
                robot_id,
                input_ctx,
                wait_device=not simulate,
                simulate=simulate,
                agent=agent,
            )
            status = result.get("status") or "failed"
            if status == "success":
                output = result.get("output") or {}
                if isinstance(output, dict):
                    text = str(output.get("text") or output.get("content") or "")
                else:
                    # workflow_engine.execute 的 output 为 output 节点文本（str）
                    text = str(output)
                return (
                    f"工作流 #{workflow_id} 执行成功"
                    + (f"，结果: {text[:1000]}" if text else "")
                )
            return (
                f"工作流 #{workflow_id} 执行失败({status}): "
                f"{result.get('error') or '未知错误'}"
            )
        except Exception as e:
            logger.warning(f"智能体运行工作流失败 agent=#{agent_id} wf={workflow_id}: {e}")
            return f"工作流执行异常: {e}"

    @staticmethod
    def _describe_schema(schema: list) -> str:
        """把输入参数声明转成给 LLM 看的文本描述"""
        if not schema:
            return ""
        parts = []
        for s in schema:
            name = str(s.get("name") or "").strip()
            if not name:
                continue
            mark = "必填" if s.get("required") else "可选"
            desc = str(s.get("description") or "")
            parts.append(f"{name}({mark}){(':' + desc) if desc else ''}")
        return "; ".join(parts)

    @staticmethod
    def _cmd_param_hint(cmd_type: int, fname: str) -> str:
        """指令参数字段的简短说明（供工具描述使用）"""
        hints = {
            "titleList": "接收房间名（多房间逗号分隔）",
            "receivedContent": "消息内容",
            "textType": "消息类型，1=文本",
            "at": "群聊真提及：填发言者昵称（与 atList 配套）",
            "atList": "群聊真提及成员昵称，多个用逗号分隔（如 张三,李四）",
            "receivedName": "原始消息发送者姓名",
            "originalContent": "原始消息内容",
            "nameList": "转发目标（逗号分隔）",
            "groupName": "群名",
            "selectList": "成员（逗号分隔）",
            "newGroupName": "新群名",
            "removeList": "踢出成员（逗号分隔）",
            "friend_name": "好友昵称/姓名（修改备注或添加好友时填写）",
            "mark_name": "新备注名（修改好友备注用法，与 friend_name 配合）",
            "friend_phone": "手机号（按手机号添加好友用法）",
            "friend_markName": "备注名（添加好友时可选）",
            "friend_markCorp": "备注企业（添加好友时可选）",
            "friend_markExtra": "备注描述（添加好友时可选）",
            "friend_leavingMsg": "好友验证附言（添加好友时可选）",
            "objectName": "文件名/企业名称/文章标题",
            "fileUrl": "文件或图片 URL",
        }
        return hints.get(fname, "")

    async def _execute_device_command_tool(
        self,
        args: dict,
        agent_id: int,
        robot_id: str,
        simulate: bool = False,
        tool_ctx: Optional[dict] = None,
    ) -> str:
        """执行设备指令工具：校验授权 → 校验必填参数 → 向当前设备下发指令 → 返回结果

        simulate=True（测试模式）：只校验授权与参数，不真实下发，返回模拟结果，
        方便在没有在线设备时验证智能体的指令调用决策。
        """
        cmd_type = int(args.get("cmd_type") or 0)
        if cmd_type <= 0:
            return "错误: 缺少有效的 cmd_type"
        allowed = set(await self.get_agent_commands(agent_id))
        if cmd_type not in allowed:
            return (
                f"错误: 智能体 #{agent_id} 未获授权下发指令 {cmd_type}"
                f"（{_CMD_LABELS.get(cmd_type, '?')}，请到「智能体管理」勾选）"
            )
        if not robot_id:
            if simulate:
                robot_id = "（测试）"
            else:
                return "错误: 当前会话未绑定设备，无法下发设备指令"
        try:
            from .aivbot_service import build_command_extra, aivbot_service

            # 校验必填参数（按指令参数契约）
            schema = _CMD_PARAM_SCHEMA.get(cmd_type, {"required": [], "optional": [], "example": {}})
            params = {}
            for k, v in args.items():
                if k != "cmd_type" and v is not None:
                    params[k] = v
            missing = [
                fname
                for fname in (schema.get("required") or [])
                if not str(params.get(fname) or "").strip()
            ]
            if missing:
                return (
                    f"错误: 指令 {cmd_type}（{_CMD_LABELS.get(cmd_type, '?')}）缺少必填参数: "
                    f"{', '.join(missing)}。请补充后再调用。参数示例: "
                    f"{json.dumps(schema.get('example') or {}, ensure_ascii=False)}"
                )
            label = _CMD_LABELS.get(cmd_type, f"指令{cmd_type}")
            # 发送前整形：friend_* 包装进 friend 对象、列表字段拆分（设备端 WeworkMessageBean.friend）
            params = build_command_extra(params)
            # 203 发送消息：带上本会话的房间类型，设备端进入房间后校验类型一致才发。
            # 起因（真机）：无自定义群名的外部群，企微头部标题是「群聊(N)」而消息列表里是
            # "成员A、成员B、成员C"，两边名字天生不一致；设备端因此无法用标题复核"是否进对房间"，
            # 只能按类型校验 —— 否则同名不同类（群"张三" vs 联系人"张三"）会发错。
            _ctx = tool_ctx or {}
            # 只在 room_type 可信（来自设备入站事件，即自动回复链路）时才下发：
            # 工作流节点/任务动作/测试页的 room_type 是配置里猜的（任务默认还给 1=外部群），
            # 拿它做校验会让"往私聊发消息"这类任务被设备端按类型不符拒掉 —— 那是倒退。
            if cmd_type == 203 and _ctx.get("room_type") and _ctx.get("room_type_trusted"):
                params.setdefault("roomType", int(_ctx["room_type"]))
            if simulate:
                return (
                    f"（模拟）设备指令校验通过：{cmd_type} {label}，参数 "
                    f"{json.dumps(params, ensure_ascii=False)}。"
                    f"测试模式未真实下发，如需真实执行请取消模拟并选择在线设备。"
                )
            ok = await aivbot_service.send_command(robot_id, cmd_type, params)
            if not ok:
                return f"错误: 设备 {robot_id} 离线或连接未就绪，指令未下发"

            # 下发成功后等待设备结果：获取类指令（501/502/…）等数据上报，
            # 其余等 socketType=3 执行回执（复用工作流引擎的等待机制）
            from .workflow_engine import workflow_engine as _wf_engine, _CMD_DATA_KEYS

            wait_sec = float(args.get("wait_sec") or 15)
            data_key = _CMD_DATA_KEYS.get(cmd_type)
            keys = [data_key, "last_result"] if data_key else ["last_result"]
            result = await _wf_engine._wait_any_result(robot_id, keys, wait_sec)
            if result.get("error"):
                return (
                    f"设备指令已下发（{cmd_type} {label}），但等待设备结果超时（{wait_sec}s），"
                    f"设备可能已执行但未回报，请稍后查询。"
                )
            return (
                f"设备指令执行结果（{cmd_type} {label}）: "
                f"{json.dumps(result, ensure_ascii=False, default=str)[:800]}"
            )
        except Exception as e:
            logger.warning(
                f"智能体下发设备指令失败 agent=#{agent_id} robot={robot_id} cmd={cmd_type}: {e}"
            )
            return f"设备指令下发异常: {e}"

    async def _execute_table_tool(self, name: str, args: dict, agent_id: int) -> str:
        """执行表格工具（table_create / table_query / table_insert / table_update），带授权校验"""
        try:
            if name == "table_create":
                from ..models.table import TableCreateRequest

                # 纵深防御：建表需智能体授权 allow_table_create（工具注入层已剔除未授权者）
                ag = await self.get_agent(agent_id) if agent_id else None
                if not ag or not getattr(ag, "allow_table_create", False):
                    return "错误: 智能体未获授权创建表格（需在智能体配置开启「允许创建表格」）"
                tname = str(args.get("name") or "").strip()
                if not tname:
                    return "错误: 缺少 name 参数（要创建的表格名称）"
                fields = args.get("fields") or []
                if isinstance(fields, str):
                    try:
                        fields = json.loads(fields.strip())
                    except Exception:
                        return "错误: fields 不是合法 JSON 数组"
                if not isinstance(fields, list) or not fields:
                    return (
                        "错误: fields 必须为非空字段数组，如 "
                        '[{"name":"name","type":"varchar","label":"姓名","required":true}]'
                    )
                req = TableCreateRequest(
                    name=tname,
                    description=str(args.get("description") or "").strip(),
                    fields=fields,
                )
                table_id = await table_service.create_table(req)
                if agent_id:
                    await table_service.set_agent_grants(table_id, {agent_id: 1})
                return f"表格创建成功：{tname}（tbl_{table_id}），已授权智能体 #{agent_id} 读写"
            table_key = str(args.get("table") or "").strip()
            if not table_key:
                return "错误: 缺少 table 参数（表格标识 table_key，如 tbl_12）"
            # 兼容 LLM 传表格名称而非内部 table_key（名称唯一，可安全解析）
            resolved = await table_service.resolve_table_key(table_key)
            if not resolved:
                return f"错误: 找不到表格「{table_key}」（表格管理 → 授权给智能体）"
            table_key = resolved
            if name == "table_query":
                if not await table_service.check_permission(agent_id, table_key, need_write=False):
                    return (
                        f"错误: 智能体 #{agent_id} 未获授权读取表格 {table_key}"
                        "（请到「表格管理 → 授权给智能体」勾选并保存）"
                    )
                limit = int(args.get("limit") or 50)
                if limit > 200:
                    limit = 200
                rows = await table_service.query_rows(
                    table_key, where=args.get("where"), limit=limit
                )
                return json.dumps(rows, ensure_ascii=False, default=str)[
                    : TOOL_RESPONSE_MAX_CHARS
                ]
            if name == "table_insert":
                if not await table_service.check_permission(agent_id, table_key, need_write=True):
                    return (
                        f"错误: 智能体 #{agent_id} 对表格 {table_key} 无写权限"
                        "（只读授权，请改为读写后重试）"
                    )
                new_id = await table_service.insert_row(table_key, args.get("row") or {})
                return f"插入成功，新行 id={new_id}"
            if name == "table_update":
                if not await table_service.check_permission(agent_id, table_key, need_write=True):
                    return (
                        f"错误: 智能体 #{agent_id} 对表格 {table_key} 无写权限"
                        "（只读授权，请改为读写后重试）"
                    )
                ok = await table_service.update_row(
                    table_key, int(args.get("id") or 0), args.get("fields") or {}
                )
                return f"更新{'成功' if ok else '失败（行不存在或字段无效）'}"
        except Exception as e:
            return f"表格操作失败: {e}"
        return f"错误: 未知工具「{name}」"

    async def _execute_task_tool(
        self,
        args: dict,
        agent_id: int,
        robot_id: str = "",
        tool_ctx: Optional[dict] = None,
    ) -> str:
        """执行 task_create 工具：创建任务（需智能体授权 allow_task_create）。

        robot_id / org_id 自动继承（LLM 未显式填写时）：
        - robot_id：优先取当前会话设备（tool_ctx["robot_id"]，真实会话/测试运行页选择）
        - org_id：优先取会话上下文组织（tool_ctx["org_id"]），否则取智能体所属组织
        """
        from ..models.task import TaskCreateRequest
        from .task_service import parse_datetime_local, task_service

        agent = await self.get_agent(agent_id) if agent_id else None
        if agent is None or not getattr(agent, "allow_task_create", False):
            return (
                f"错误: 智能体 #{agent_id} 未被授权创建任务"
                "（智能体管理 → 允许创建任务）"
            )

        def _spec(v):
            if isinstance(v, str):
                try:
                    v = json.loads(v.strip())
                except Exception:
                    v = {}
            return v or {}

        try:
            name = str(args.get("name") or "").strip()
            if not name:
                return "错误: 缺少任务名 name"
            trigger_spec = _spec(args.get("trigger_spec"))
            # 顶层 once_at / repeat_count 并入触发配置（单次定时 / 重复次数）
            if args.get("once_at") is not None:
                once_ts = parse_datetime_local(args.get("once_at"))
                if once_ts is None:
                    return "错误: once_at 格式应为 YYYY-MM-DDTHH:MM 或秒级时间戳"
                trigger_spec["once_at"] = once_ts
            rc = args.get("repeat_count")
            if rc not in (None, ""):
                rc = int(rc)
                if rc > 0:
                    trigger_spec["repeat_count"] = rc
            # 设备绑定：LLM 未填时默认当前会话设备
            bind_robot = str(args.get("robot_id") or "").strip()
            if not bind_robot and tool_ctx:
                bind_robot = str(tool_ctx.get("robot_id") or "").strip()
            # 组织归属：LLM 未填 → 会话上下文组织 → 智能体所属组织
            org_id = int(args.get("org_id") or 0)
            if not org_id and tool_ctx:
                org_id = int(tool_ctx.get("org_id") or 0)
            if not org_id:
                try:
                    gids = await group_service.get_member_org_groups("agent", agent_id)
                    org_id = gids[0] if gids else 0
                except Exception:
                    org_id = 0
            req = TaskCreateRequest(
                name=name,
                org_id=org_id,
                robot_id=bind_robot,
                trigger_type=str(args.get("trigger_type") or "").strip(),
                trigger_spec=trigger_spec,
                action_type=str(args.get("action_type") or "").strip(),
                action_spec=_spec(args.get("action_spec")),
                enabled=bool(args.get("enabled", True)),
                expires_at=int(args.get("expires_at") or 0),
            )
            ok, res = await task_service.create_task(req)
            if not ok:
                return f"错误: {res}"
            return f"任务创建成功！任务 id={res}（{name}）"
        except Exception as e:
            return f"任务创建失败: {e}"

    async def test_connection(self, agent: Agent) -> Tuple[bool, str]:
        """测试智能体连接（发送一条最小请求验证 key/地址/模型）"""
        try:
            reply = await self._chat_completion(
                agent, [{"role": "user", "content": "请回复：连接成功"}]
            )
            return True, reply[:100]
        except Exception as e:
            return False, str(e)

    # ── 会话管理与回复生成 ──────────────────────────────

    @staticmethod
    def _trim_history(history: List[dict], max_turns: int) -> List[dict]:
        """
        控制对话长度：最多保留 max_turns 轮（1 轮 = 1 条用户消息 + 1 条助手回复）。
        超出时从最旧的开始成对丢弃。
        """
        if max_turns < 1:
            return []
        while True:
            user_idx = [i for i, m in enumerate(history) if m.get("role") == "user"]
            if len(user_idx) <= max_turns:
                break
            i = user_idx[0]
            del history[i]
            # 尽量成对丢弃：紧随其后的助手回复一并删除
            if i < len(history) and history[i].get("role") == "assistant":
                del history[i]
        # 理论上不会出现，兜底：开头不允许是孤立的 assistant
        while history and history[0].get("role") == "assistant":
            history.pop(0)
        return history

    # ── 图片消息处理（策略 0=路径 1=Base64 视觉 2=OCR）──────────

    async def _build_user_content(
        self, agent: Agent, user_text: str, image_paths: List[str]
    ) -> Tuple[Any, str]:
        """
        按 agent.image_strategy 组装本轮用户消息：
        返回 (发送给 LLM 的 content, 存入历史的纯文本 content)。
        - 策略 0：图片转为 "[图片] 路径" 文本
        - 策略 1：图片压缩后转 Base64 data URI，构造 OpenAI 兼容多模态内容块
        - 策略 2：PaddleOCR 提取图片文字后作为文本
        """
        text_part = user_text or ""
        if not image_paths:
            return text_part, text_part

        strategy = int(agent.image_strategy or 0)

        # 策略 0：传递路径
        if strategy == 0:
            merged = (text_part + "\n" + "\n".join(f"[图片] {p}" for p in image_paths)).strip()
            return merged, merged

        # 策略 2：OCR 识别图片文字
        if strategy == 2:
            ocr_lines = []
            for p in image_paths:
                abs_path = self._resolve_upload_path(p)
                if not abs_path or not os.path.exists(abs_path):
                    ocr_lines.append(f"[图片] {p}")
                    continue
                try:
                    text = await self._ocr_image(abs_path)
                    if text:
                        ocr_lines.append(f"[图片文字识别] {text}")
                    else:
                        ocr_lines.append(f"[图片(未识别到文字)] {p}")
                except Exception as e:
                    logger.warning(f"图片 OCR 失败 {p}: {e}")
                    ocr_lines.append(f"[图片] {p}")
            merged = (text_part + "\n" + "\n".join(ocr_lines)).strip()
            return merged, merged

        # 策略 1：Base64 视觉（多模态内容块数组）
        blocks = []
        if text_part:
            blocks.append({"type": "text", "text": text_part})
        for p in image_paths:
            abs_path = self._resolve_upload_path(p)
            data_uri = None
            if abs_path and os.path.exists(abs_path):
                try:
                    data_uri = self._image_to_base64_data_uri(abs_path)
                except Exception as e:
                    logger.warning(f"图片读取/压缩失败 {p}: {e}")
            if data_uri:
                blocks.append(
                    {"type": "image_url", "image_url": {"url": data_uri, "detail": "auto"}}
                )
            else:
                blocks.append({"type": "text", "text": f"[图片] {p}"})
        # 全部图片都读取失败 → 退化为纯文本
        if len(blocks) == 1 and blocks[0]["type"] == "text":
            return blocks[0]["text"], blocks[0]["text"]
        history_text = (
            text_part + "\n" + "\n".join(f"[图片] {p}" for p in image_paths)
        ).strip()
        return blocks, history_text

    @staticmethod
    def _resolve_upload_path(path: str) -> Optional[str]:
        """imagePath 是相对项目根的 uploads/... 路径，转为服务器绝对路径"""
        if not path:
            return None
        path = path.replace("\\", "/")
        if os.path.isabs(path):
            return path
        return os.path.join(PROJECT_ROOT, path.lstrip("/"))

    @staticmethod
    def _image_to_base64_data_uri(abs_path: str) -> str:
        """读取图片，压缩到最长边 1024（控 token 成本）后转 Base64 data URI"""
        from PIL import Image

        mime = mimetypes.guess_type(abs_path)[0] or "image/png"
        with Image.open(abs_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            max_side = 1024
            if max(w, h) > max_side:
                ratio = max_side / float(max(w, h))
                im = im.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            buf = BytesIO()
            im.save(buf, format="JPEG", quality=85)
            data = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{data}"

    async def _ocr_image(self, abs_path: str) -> str:
        """使用 PaddleOCR 提取图片文字（懒加载；CPU 推理放线程池，避免阻塞事件循环）"""
        global _ocr_engine
        if _ocr_engine is None:
            try:
                from paddleocr import PaddleOCR
            except ImportError as e:
                raise RuntimeError(
                    "未安装 PaddleOCR，请执行 pip install paddleocr（推理后端 onnxruntime）"
                ) from e
            try:
                # paddleocr 3.x：显式 ONNX Runtime 后端（Python 3.14 无 paddlepaddle wheel，必须用 onnx）
                _ocr_engine = PaddleOCR(lang="ch", engine="onnxruntime")
            except (TypeError, ValueError):
                # paddleocr 2.x 参数
                _ocr_engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        try:
            result = await asyncio.to_thread(_ocr_engine.ocr, abs_path, True)
        except (AttributeError, TypeError):
            result = await asyncio.to_thread(_ocr_engine.predict, abs_path)
        texts = _extract_ocr_texts(result)
        return "\n".join(texts)

    async def generate_reply(
        self,
        agent: Agent,
        robot_id: str,
        room_name: str,
        room_type: int,
        user_text: str,
        image_paths: Optional[List[str]] = None,
        simulate: bool = False,
        trace: Optional[list] = None,
        org_id: int = 0,
        speaker: Optional[str] = None,
        reply_policy: str = "auto",
        room_type_trusted: bool = False,
        self_name: str = "",
    ) -> Optional[str]:
        """
        按会话管理规则生成智能体回复：
        1. 读取 (robot_id, room_name, speaker) 的历史上下文（群聊按发言者隔离）
        2. 换智能体 / 空闲超时 → 重置上下文
        3. 拼接 system + 历史 + 新消息，调用 LLM
           （图片消息按 agent.image_strategy 处理：0=传路径 1=传 Base64 图 2=OCR 识别文字）
        4. 追加本轮对话并裁剪到 max_context_turns 轮，落库
        失败返回 None（调用方不发送任何内容）。

        simulate：测试模式，透传给工具执行（工作流设备节点/设备指令不依赖真实设备）。
        trace：可选的列表，非 None 时记录每次工具调用的 (工具名, 参数, 结果)，
               供「智能体测试运行」页面展示。
        speaker：群聊发言者（自动回复 v2），非空时给消息加「对方：xxx」前缀、追加回复规则、
                 并按发言者隔离会话历史；LLM 判断无需回复时返回空串 → 本函数返回 None
                 （不发送任何内容、不写历史）。
        reply_policy：回复归属，决定注入给 LLM 的回复规则（回复由智能体自己通过设备指令
                 发出，服务器不代发本函数的返回值）：
                 - auto（默认，设备聊天）：是否回复由模型判断，要回复须调用 203
                 - reply（任务 source/custom）：必须回复到 room_name 指定的会话
                 - none（任务 none）：本次不要发送任何消息，只完成指令要求的其他操作
        """
        user_text = (user_text or "").strip()
        image_paths = [p for p in (image_paths or []) if p]
        if not user_text and not image_paths:
            return None

        # 群聊（1=外部群 3=内部群）按发言者隔离会话历史，避免同群多成员上下文互相污染；私聊不隔离
        conv_speaker = speaker if (int(room_type) in (1, 3) and speaker) else ""

        # 1. 读取历史
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT agent_id, history, last_msg_at
                       FROM agent_conversations
                       WHERE robot_id=%s AND room_name=%s AND speaker=%s""",
                    (robot_id, room_name, conv_speaker),
                )
                row = await cur.fetchone()

        history: List[dict] = []
        if row:
            conv_agent_id, raw_history, last_msg_at = row[0], row[1], row[2]
            try:
                history = json.loads(raw_history) if raw_history else []
                if not isinstance(history, list):
                    history = []
            except Exception:
                history = []
            # 2a. 更换了绑定智能体 → 全新会话
            if conv_agent_id != agent.id:
                history = []
            # 2b. 空闲超时 → 开启新会话（避免上下文无限累积、过期话题干扰）
            if history and agent.idle_reset_minutes > 0 and last_msg_at:
                idle_seconds = (datetime.now() - last_msg_at).total_seconds()
                if idle_seconds > agent.idle_reset_minutes * 60:
                    logger.info(
                        f"会话空闲 {int(idle_seconds / 60)} 分钟，自动开启新会话: {robot_id} / {room_name}"
                    )
                    history = []

        # 3. 按图片策略组装本轮用户消息内容
        #    user_msg_content：发给 LLM 的 content（多模态时可为一组内容块）
        #    history_user_content：仅文本的摘要（存库防 base64 膨胀、防对话过长）
        user_msg_content, history_user_content = await self._build_user_content(
            agent, user_text, image_paths
        )
        # 发言者前缀：让 LLM 知道当前在跟谁对话（文本消息加前缀；多模态内容块头部插文本块）
        if speaker:
            speaker_prefix = f"对方：{speaker}"
            if isinstance(user_msg_content, str):
                user_msg_content = f"{speaker_prefix}\n{user_msg_content}"
            else:
                user_msg_content = [
                    {"type": "text", "text": speaker_prefix}
                ] + list(user_msg_content)
            history_user_content = f"{speaker_prefix}\n{history_user_content}"
        # 知识库检索（RAG）：有文本问题时检索智能体关联的知识库，命中内容注入 system
        rag_context = ""
        if user_text:
            try:
                rag_context = await knowledge_service.build_rag_context(
                    agent.id, user_text
                )
            except Exception as e:
                logger.warning(f"知识库检索失败 agent=#{agent.id}: {e}")

        # 长期记忆：按当前会话线索（私聊=会话名、群聊=发言者）精确命中客户，注入其事实 + 记忆规则
        # 放在 system_text 组装之后：记忆块要拼在 RAG 之后，不能被后面的赋值覆盖
        messages = []
        system_text = agent.system_prompt or ""
        if rag_context:
            system_text = (
                f"{system_text}\n\n" if system_text else ""
            ) + (
                "【知识库参考资料】\n"
                f"{rag_context}\n\n"
                "请优先参考上述资料回答；若资料与当前问题无关，忽略资料按自身知识回答。"
            )
        try:
            memory_context = await memory_service.build_memory_context(
                robot_id, room_name or "", speaker or "", int(room_type)
            )
        except Exception as e:
            memory_context = ""
            logger.warning(f"长期记忆读取失败 robot={robot_id}: {e}", exc_info=True)
        if memory_context:
            system_text = (
                f"{system_text}\n\n" if system_text else ""
            ) + memory_context
        # 技能触发注入：按会话上下文匹配 skill，命中则注入行为指导（软，不承载安全强制）
        try:
            matched_skills = await skill_service.match_skills(
                agent.id, int(room_type), room_name, speaker or "", user_text
            )
            if matched_skills:
                skill_block = "\n\n".join(
                    f"【技能：{s['name']}】\n{s['content']}" for s in matched_skills
                )
                system_text = (
                    f"{system_text}\n\n" if system_text else ""
                ) + skill_block
        except Exception as e:
            # exc_info：技能链路曾因调用签名不匹配（TypeError）整条静默失效，只留一行消息查不出来
            logger.warning(f"技能匹配失败: {e}", exc_info=True)
        # 群聊回复规则：LLM 自主判断值不值得回复（返回空串则不发送）；@发言者
        if speaker:
            system_text = (
                f"{system_text}\n\n" if system_text else ""
            ) + (
                "【回复规则】\n"
                "对方可能发送了多条消息，请一并回应；若消息无实质内容、与当前话题无关或无需回复，"
                f"请直接返回空字符串，服务端将不发送任何内容。群聊回复请以 @{speaker} 开头。"
            )
        # ── 当前会话上下文 + 回复方式 ──
        # 设计：回复由智能体自己通过设备指令 203 发出，服务器不再代发本函数的返回值
        # （返回值只用于日志/历史/任务记录，永不回发给客户）。
        # 之前不注入会话名，模型为了拿到 203 必填的 titleList 只能调 505/502/503 去找房间，
        # 表现成「查通讯录→找聊天室→回复→再查企业信息」的绕路。
        is_group_session = int(room_type) in (1, 3)
        policy = (reply_policy or "auto").strip().lower()
        session_lines = [
            "【当前会话】",
            f"- 会话名称：{room_name or '（未知）'}",
            f"- 会话类型：{'群聊' if is_group_session else '私聊'}",
        ]
        if speaker:
            session_lines.append(f"- 当前发言者：{speaker}")
        # 群聊 @ 归属澄清：**只在本批次正文真的 @ 了本机时才注入**。
        # 不这么做会有两个副作用（都是真实策略语义）：
        #   ① track_at 模式是"被 @ 后 180s 内该成员的消息都交给模型判断是否回复"，
        #      若无条件写"@我就必须回"，追踪窗口里那些**没 @ 的后续消息**也会被要求必回；
        #   ② 一个批次是"该成员多条消息拼成一条 user 消息"（aivbot_service 用 \n 拼接），
        #      只提 @ 会诱导模型只回 @ 那条、忽略同一批次里的其他消息。
        if is_group_session and policy != "none":
            at_me = mentions_me(user_text or "", self_name)
            if self_name and at_me:
                session_lines.append(
                    f"- 你在企业微信里的显示名：{self_name}。本批消息里的 @{self_name} 是客户在 @你"
                    "（不是 @别人）——不要以「@ 的不是我」为理由不回。"
                )
                session_lines.append(
                    "- 但要不要回、回什么，仍按【回复规则】判断：本批可能包含该客户的**多条**消息"
                    "（@ 之后的追问也在里面），请整体判断、一并回应；若确实没有实质诉求"
                    "（只是 @ 一下、或纯寒暄），仍可返回空字符串。"
                )
            elif not self_name and any(ch in (user_text or "") for ch in _AT_CHARS):
                session_lines.append(
                    "- 本批正文里有 @：服务端只会把 @ 到你的消息（或你正在追踪的成员的消息）转给你，"
                    "不要仅因为 @ 的是别人就判定「无需回复」；是否回复仍按【回复规则】整体判断。"
                )
        session_lines.append("")
        session_lines.append("【回复方式（必须遵守）】")
        if policy == "none":
            session_lines += [
                "- 本次不需要回复：不要调用 device_command_203/204/205，也不要发送任何消息；"
                "只完成指令要求的其他操作（如写入表格）。",
                "- 完成后请用一句话说明你执行了什么，作为你的最终回答。",
            ]
        else:
            session_lines += [
                "- 上面的「会话名称」就是当前会话，直接用它；"
                "不要为了找会话或确认身份去调用 505（最近聊天）/502（好友资料）/503（我的信息）。",
                "- 要回复时必须调用 device_command_203（发送消息）："
                f"titleList 填「{room_name}」，receivedContent 填回复正文，textType 填 \"1\"。",
            ]
            if is_group_session and speaker:
                session_lines.append(
                    "- 群聊回复须在 203 中携带 at（发言者昵称）与 atList（发言者昵称，"
                    "多人用逗号分隔），正文以「@发言者 」开头。"
                )
            elif is_group_session:
                session_lines.append(
                    "- 本次在群聊里，但服务器没能解析出发言者昵称：直接发送正文即可，"
                    "不要编造 @对象、也不要为了查昵称去调用 501/502/505。"
                )
            if policy == "reply":
                session_lines.append("- 本次必须回复，且只回复到上面的会话名称对应的会话。")
            else:
                session_lines.append(
                    "- 是否回复由你判断：消息无实质内容、与当前话题无关或无需回复时，"
                    "不要调用 203，直接返回空字符串。"
                )
            session_lines.append(
                "- 通过 203 发送成功后，请把发送的正文作为你的最终回答再输出一份"
                "（仅用于服务端记录与审计，服务端不会代发，请不要因此改用普通文本回复）。"
            )
        system_text = (
            f"{system_text}\n\n" if system_text else ""
        ) + "\n".join(session_lines)
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.extend(history)
        messages.append({"role": "user", "content": user_msg_content})

        # 会话上下文：智能体触发工作流时自动注入为 input（{{input.user_text}} 等模板变量可用）；
        # robot_id/org_id 供 task_create 等工具在 LLM 未显式传参时自动继承
        tool_ctx = {
            "robot_id": robot_id,
            "room_name": room_name,
            "room_type": int(room_type),
            # room_type 是"从设备真实入站事件读到的"还是"调用方配置里猜的"：
            # 只有前者才值得下发给设备做类型校验（见 _execute_device_command_tool 的 203 分支）
            "room_type_trusted": bool(room_type_trusted),
            "speaker": speaker or "",
            "user_text": user_text,
            "org_id": org_id,
        }

        try:
            reply = await self._chat_completion(
                agent,
                messages,
                robot_id=robot_id,
                tool_ctx=tool_ctx,
                simulate=simulate,
                trace=trace,
            )
        except Exception as e:
            # Base64 视觉（多模态）失败 → 降级为纯文本（路径/OCR 文本）重试一次，
            # 避免所选模型不支持图片时整轮无回复
            if isinstance(user_msg_content, list):
                logger.warning(
                    f"多模态调用失败，降级为文本重试 robot={robot_id} room={room_name}: {e}"
                )
                messages = messages[:-1] + [
                    {"role": "user", "content": history_user_content}
                ]
                try:
                    reply = await self._chat_completion(
                        agent,
                        messages,
                        robot_id=robot_id,
                        tool_ctx=tool_ctx,
                        simulate=simulate,
                        trace=trace,
                    )
                except Exception as e2:
                    logger.error(
                        f"LLM 调用失败 robot={robot_id} room={room_name} agent=#{agent.id}: {e2}"
                    )
                    return None
            else:
                logger.error(
                    f"LLM 调用失败 robot={robot_id} room={room_name} agent=#{agent.id}: {e}"
                )
                return None

        # 空回复守卫：LLM 判断无需回复时返回空串 → 不写历史、不发送任何内容
        if not reply or not str(reply).strip():
            return None

        # 4. 追加本轮并裁剪落库（历史只存文本，不存 base64）
        history.append({"role": "user", "content": history_user_content})
        history.append({"role": "assistant", "content": reply})
        history = self._trim_history(history, agent.max_context_turns)

        history_json = json.dumps(history, ensure_ascii=False)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO agent_conversations
                           (agent_id, robot_id, room_name, speaker, room_type, history, last_msg_at)
                       VALUES (%s,%s,%s,%s,%s,%s,NOW())
                       ON DUPLICATE KEY UPDATE
                           agent_id=%s, history=%s, room_type=%s, speaker=%s, last_msg_at=NOW()""",
                    (
                        agent.id,
                        robot_id,
                        room_name,
                        conv_speaker,
                        int(room_type),
                        history_json,
                        agent.id,
                        history_json,
                        int(room_type),
                        conv_speaker,
                    ),
                )
                await conn.commit()

        return reply


# 全局服务实例（pool 在 startup 时注入）
agent_service = AgentService()


def _extract_ocr_texts(result) -> List[str]:
    """递归提取 PaddleOCR 输出中的文字（兼容 2.x 的 [[box,(text,score)],...] 与 3.x 的 OCRResult）"""
    texts: List[str] = []

    def walk(obj):
        if isinstance(obj, dict):
            for k in ("rec_texts", "texts", "text"):
                if k in obj and isinstance(obj[k], list):
                    for x in obj[k]:
                        if isinstance(x, str) and x.strip():
                            texts.append(x.strip())
            for v in obj.values():
                walk(v)
        elif isinstance(obj, (list, tuple)):
            # (text, score) 元组
            if len(obj) == 2 and isinstance(obj[0], str) and isinstance(
                obj[1], (int, float)
            ):
                if obj[0].strip():
                    texts.append(obj[0].strip())
                return
            for v in obj:
                walk(v)
        elif hasattr(obj, "json"):
            try:
                # 3.x 的 OCRResult.json 是 dict 属性；2.x 为方法，均兼容
                d = obj.json
                if callable(d):
                    d = d()
                if isinstance(d, dict):
                    walk(d)
            except Exception:
                pass

    walk(result)
    # 去重保序（PaddleOCR 对同一行文字可能重复输出）
    seen, out = set(), []
    for t in texts:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out
