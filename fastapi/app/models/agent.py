"""
智能体（Agent）数据模型
"""

from typing import Optional
from pydantic import BaseModel, Field


class AgentCreateRequest(BaseModel):
    """创建智能体请求模型（更新时 api_key 传空串表示保留原 key）"""

    name: str = Field(..., min_length=1, max_length=100, description="智能体名称")
    description: str = Field(default="", max_length=500, description="描述")
    provider: str = Field(default="custom", description="LLM 服务商标识（deepseek/openai/qwen/zhipu/moonshot/custom）")
    base_url: str = Field(default="", description="OpenAI 兼容接口基础地址，如 https://api.deepseek.com/v1")
    model: str = Field(default="", description="模型名称，如 deepseek-flash")
    api_key: str = Field(default="", description="API Key")
    system_prompt: str = Field(default="", description="系统提示词（角色设定）")
    temperature: float = Field(default=0.7, ge=0, le=2, description="采样温度")
    # 默认 8192：思考模式的 reasoning token 也占这个预算，1024 会把预算全耗在思考上、正文为空
    max_tokens: int = Field(default=8192, ge=1, le=32768, description="单次回复最大 token 数（含思考 token）")
    max_context_turns: int = Field(default=20, ge=1, le=200, description="会话上下文保留轮数（对话过长时丢弃最旧）")
    idle_reset_minutes: int = Field(default=30, ge=0, le=1440, description="空闲 N 分钟后重置会话（0=不自动重置）")
    image_strategy: int = Field(default=0, ge=0, le=2, description="图片消息处理策略：0=传递路径 1=传递图片(Base64视觉识别) 2=OCR识别图片文字")
    enable_tools: bool = Field(default=True, description="启用工具（Function Calling；界面总开关已移除，创建/编辑时自动开启，具体能力由各子开关与授权决定）")
    enable_http: bool = Field(default=False, description="启用联网工具（http_get/http_post）")
    allow_table_create: bool = Field(default=False, description="允许创建表格（创建后自动授权本智能体读写）")
    allow_task_create: bool = Field(default=False, description="允许创建任务（授权后 LLM 可调用 task_create 工具创建定时/条件任务）")
    enabled: bool = Field(default=True, description="是否启用（停用的智能体不回复）")


class AgentUpdateRequest(AgentCreateRequest):
    """更新智能体请求模型（api_key 为空表示保留原 key）"""

    api_key: str = Field(default="", description="API Key（留空表示保留原 Key）")


class AgentBindRequest(BaseModel):
    """设备绑定智能体请求模型"""

    agent_id: Optional[int] = Field(default=None, description="智能体 ID，传 null/0 解绑")


class Agent(BaseModel):
    """智能体信息模型"""

    id: int = Field(..., description="智能体 ID")
    name: str = Field(..., description="智能体名称")
    description: str = Field(default="", description="描述")
    provider: str = Field(default="custom", description="LLM 服务商标识")
    base_url: str = Field(default="", description="API 基础地址")
    model: str = Field(default="", description="模型名称")
    api_key: str = Field(default="", description="API Key")
    system_prompt: str = Field(default="", description="系统提示词")
    temperature: float = Field(default=0.7, description="采样温度")
    max_tokens: int = Field(default=8192, description="最大输出 token 数")
    max_context_turns: int = Field(default=20, description="上下文保留轮数")
    idle_reset_minutes: int = Field(default=30, description="空闲重置分钟数")
    image_strategy: int = Field(default=0, description="图片消息处理策略：0=传递路径 1=传递图片(Base64视觉识别) 2=OCR识别图片文字")
    enable_tools: bool = Field(default=True, description="启用工具（Function Calling；界面总开关已移除，创建/编辑时自动开启，具体能力由各子开关与授权决定）")
    enable_http: bool = Field(default=False, description="启用联网工具（http_get/http_post）")
    allow_table_create: bool = Field(default=False, description="允许创建表格（创建后自动授权本智能体读写）")
    allow_task_create: bool = Field(default=False, description="允许创建任务（授权后 LLM 可调用 task_create 工具创建定时/条件任务）")
    enabled: bool = Field(default=True, description="是否启用")
    created_at: str = Field(default="", description="创建时间")
    robot_count: int = Field(default=0, description="绑定该智能体的设备数")
    kind: str = Field(default="chat", description="用途：chat=普通对话智能体，provisioner=组织配置智能体（不出现在组织资源列表/下拉里）")
