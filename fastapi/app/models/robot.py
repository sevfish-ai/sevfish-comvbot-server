"""
Robot device registration models
"""

from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime


class RobotStatus(str, Enum):
    """Robot status enumeration"""

    OFFLINE = "offline"
    ONLINE = "online"
    BANNED = "banned"


class RobotRegisterRequest(BaseModel):
    """Robot registration request model"""

    device_name: Optional[str] = Field(default="", description="Device name/alias")
    remark: Optional[str] = Field(default="", description="Remark")


class RobotRegisterResponse(BaseModel):
    """Robot registration response model"""

    robot_id: str = Field(..., description="Unique robot ID")
    token: str = Field(..., description="Authentication token")


class RobotInfo(BaseModel):
    """Robot device information model"""

    robot_id: str = Field(..., description="Unique robot ID")
    token: str = Field(..., description="Authentication token")
    device_name: str = Field(default="", description="Device name")
    remark: str = Field(default="", description="Remark")
    status: RobotStatus = Field(
        default=RobotStatus.OFFLINE, description="Current status"
    )
    last_seen: Optional[str] = Field(default=None, description="Last connection time")
    created_at: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="Registration time",
    )
    ws_url: str = Field(default="", description="WebSocket URL template")
    reply_all: int = Field(default=1, description="Reply strategy: -1=readonly 0=@only 1=reply all")
    reply_strategy_id: Optional[int] = Field(default=None, description="Reply strategy group ID")
    strategy_name: str = Field(default="", description="Reply strategy group name")
    agent_id: Optional[int] = Field(default=None, description="Bound agent ID (null=unbound)")
    agent_name: str = Field(default="", description="Bound agent name")
    auto_reply: bool = Field(default=True, description="Auto reply switch (server-managed)")


class ReplyStrategy(BaseModel):
    """回复策略组模型（四类聊天室配置 + 设备级限额，v2）

    config: {room_type: {mode, merge_window[, track_duration]}}
      room_type: "1"=外部群 "2"=外部私聊 "3"=内部群 "4"=内部私聊
      mode: 私聊 reply|silent；群聊 track_at|at_only|reply_all|silent
    reply_all: 兼容摘要（供旧接口/文档用）：-1=全部不回复 0=仅@ 1=有回复
    """

    id: int = Field(..., description="Strategy group ID")
    name: str = Field(..., description="Strategy group name")
    config: dict = Field(
        default_factory=dict,
        description="四类聊天室配置 {room_type: {mode, merge_window[, track_duration]}}",
    )
    max_concurrent_llm: int = Field(default=2, description="设备同时最大 LLM 调用数")
    max_calls_per_min: int = Field(default=5, description="设备 60s 内最大 LLM 调用数")
    reply_all: int = Field(default=1, description="兼容摘要：-1=全部不回复 0=仅@ 1=有回复")
    is_default: bool = Field(default=False, description="Default strategy (not editable/deletable)")
    created_at: str = Field(default="", description="Created time")
    robot_count: int = Field(default=0, description="Devices using this strategy")


class RobotListResponse(BaseModel):
    """Robot list response model"""

    total: int = Field(..., description="Total count")
    robots: List[RobotInfo] = Field(default_factory=list, description="Robot list")


class ApiResponse(BaseModel):
    """Generic API response model"""

    status: str = Field(..., description="Status: success/error")
    message: str = Field(default="", description="Message")


class ApiDataResponse(BaseModel):
    """API response with data"""

    status: str = Field(..., description="Status: success/error")
    data: dict = Field(default_factory=dict, description="Data")
    message: str = Field(default="", description="Message")
