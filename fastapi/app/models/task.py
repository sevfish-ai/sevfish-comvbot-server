"""
任务模块数据模型

任务 = 触发器 × 动作（两维正交）
- 触发器 trigger_type:
    schedule   定时触发：trigger_spec = {"cron": "0 9 * * *"} 或 {"interval_sec": 3600}
                        单次执行：{"once_at": 时间戳}；重复次数：{"repeat_count": N}（0/缺省=无限）
                        延迟开始（工作流节点）：{"offset_sec": 秒}，从当前时间偏移后起算首次时间
    condition  条件触发：trigger_spec = {"type": "friend_accepted", "target": {...}} 
                            或 {"type": "message_from", "target": "对方昵称"}
- 动作 action_type:
    workflow  执行工作流：action_spec = {"workflow_id": 3, "params": {...}}
    agent     向智能体注入指令：action_spec = {"agent_id": 2, "instruction": "...", 
                                              "room": "", "room_type": 1, "send_reply": "none"}
             send_reply 三态：none=不发送 / source=回复到触发事件会话 / custom=回复到自定义会话（此时 room 必填）。
             旧布尔数据兼容：True→custom（room 有值）或 source（room 为空），False→none

任务结束（state=done + 自动停用）：
- 条件任务：oneshot 成功后
- 定时任务：once_at 单次执行后；repeat_count 重复次数耗尽后

条件检测三层策略（见 task_service.py 模块注释）：
  1. 事件流   —— 设备端主动上报（101 收到消息 / 502 好友资料），零额外成本
  2. 轻量信号 —— message_from 直接由 101 消息流匹配；friend_accepted 由 502 命中
  3. 共享轮询 —— friend_accepted 兜底：每设备一个低频后台轮询器，507 全量好友快照 diff
"""
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

# 触发器 / 动作类型常量
TRIGGER_SCHEDULE = "schedule"
TRIGGER_CONDITION = "condition"
ACTION_WORKFLOW = "workflow"
ACTION_AGENT = "agent"

# 条件类型
COND_FRIEND_ACCEPTED = "friend_accepted"   # 好友申请通过
COND_MESSAGE_FROM = "message_from"          # 收到某人消息

# 任务状态
STATE_IDLE = "idle"        # 等待下一次触发
STATE_WAITING = "waiting"  # 条件任务：等待条件满足
STATE_RUNNING = "running"  # 正在执行
STATE_DONE = "done"        # 条件任务：已完成（一次性）


class TaskCreateRequest(BaseModel):
    """创建任务请求"""

    name: str = Field(..., max_length=100, description="任务名")
    org_id: int = Field(default=0, description="组织 ID")
    robot_id: str = Field(default="", max_length=10, description="设备 ID（条件触发必填）")
    trigger_type: str = Field(..., description="schedule | condition")
    trigger_spec: Dict[str, Any] = Field(default_factory=dict, description="触发配置")
    action_type: str = Field(..., description="workflow | agent")
    action_spec: Dict[str, Any] = Field(default_factory=dict, description="动作配置")
    enabled: bool = Field(default=True, description="是否启用")
    expires_at: int = Field(default=0, description="条件任务过期时间戳（0=不过期）")


class TaskUpdateRequest(BaseModel):
    """更新任务请求（全部可选，仅更新非空字段）"""

    name: Optional[str] = Field(default=None, max_length=100)
    robot_id: Optional[str] = Field(default=None, max_length=10)
    trigger_spec: Optional[Dict[str, Any]] = None
    action_spec: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    expires_at: Optional[int] = None
