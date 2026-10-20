"""
命令工作流数据模型

工作流 = 节点图：每个节点显式声明每个出口的去向（节点名 / 到此结束 / 中止），
**顺序不参与运行语义**，只决定画布上从哪个节点开始。
触发方式（触发词/命令路由）暂缓，先提供完整的工作流编辑 + 手动测试运行。
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

# 节点类型注册表（与 workflow_engine 保持一致）
NODE_TYPES = [
    ("llm", "LLM 分析", "调用 LLM，按提示词模板生成文本"),
    ("table_query", "查询表格", "查询已授权自定义表格（只读）"),
    ("table_insert", "插入表格", "向授权表格插入一行（需写权限）"),
    ("table_update", "更新表格", "更新授权表格某行（需写权限）"),
    ("table_delete", "删除表格", "删除授权表格某行（需写权限）"),
    ("table_create", "创建表格", "创建自定义表格（字段可自定义，创建后自动授权绑定智能体）"),
    ("http_get", "联网 GET", "GET 访问 URL 获取内容"),
    ("http_post", "联网 POST", "POST 提交 JSON 数据并获取内容"),
    ("device_command", "设备指令", "向绑定设备下发 Aivbot 指令（发消息/取好友/取群等）"),
    ("task_create", "创建任务", "创建定时/条件触发任务（复用任务管理逻辑）"),
    ("condition", "条件判断", "判断上一步数据是否满足条件，满足走成功分支"),
    ("switch", "多分支判断", "按判断字段的值从上到下匹配多个分支，命中哪个分支就走哪个"),
    ("code", "代码节点", "运行 Python 代码处理数据，通过 output 变量控制节点输出"),
    ("output", "输出回复", "生成最终回复文本（{{变量}} 模板）"),
]

NODE_TYPE_NAMES = [n[0] for n in NODE_TYPES]

# 跳转目标保留值：表示「流程到此结束」。
# 为什么需要它：没有它，链尾节点只能靠"排在最后"来结束流程，一旦后面又追加了别的
# 分支的节点，就会被串上去。分支配出去的节点默认就带这个值，链尾才是真正的链尾。
END_TARGET = "__end__"
# 「新增节点」表单的默认去向：自动接在列表最后一个节点后面。
# 显式做成一个选项而不是偷偷改上一个节点——否则用户明明把上个节点设成"到此结束"，
# 再加个节点就会被莫名改掉。
AUTO_LINK = "__auto__"
# 不允许用作节点名（否则跳转解析会撞车）
RESERVED_NODE_NAMES = {END_TARGET, AUTO_LINK}


class WorkflowCreateRequest(BaseModel):
    """创建工作流请求模型"""

    name: str = Field(..., min_length=1, max_length=100, description="工作流名称")
    description: str = Field(default="", max_length=500, description="描述")
    timeout_sec: int = Field(default=60, ge=5, le=600, description="整条工作流超时（秒）")
    is_sensitive: bool = Field(
        default=False,
        description="敏感标记：涉及删除/导出/外部副作用等高风险操作时开启，"
        "授权给对话智能体时管理后台会给出警告（仅提示与审计，不强制拦截）",
    )
    input_schema: Optional[str] = Field(
        default=None,
        description="输入参数声明（JSON 字符串）。数组元素: {\"name\",\"type\",\"required\",\"description\"}，如 "
        "[{\"name\":\"user_text\",\"type\":\"string\",\"required\":true,\"description\":\"用户查询内容\"}]。"
        "智能体调用工作流时按此声明传参，未声明则不必传。",
    )


class WorkflowInfo(BaseModel):
    """工作流信息模型"""

    id: int = Field(..., description="工作流 ID")
    name: str = Field(..., description="工作流名称")
    description: str = Field(default="", description="描述")
    enabled: bool = Field(default=True, description="是否启用")
    is_sensitive: bool = Field(default=False, description="敏感标记（高风险操作，授权对话智能体时警告）")
    timeout_sec: int = Field(default=60, description="超时（秒）")
    input_schema: Optional[str] = Field(default=None, description="输入参数声明（JSON 字符串）")
    node_count: int = Field(default=0, description="节点数")
    created_at: str = Field(default="", description="创建时间")


class WorkflowNodeRequest(BaseModel):
    """工作流节点请求模型"""

    node_type: str = Field(..., description="节点类型")
    name: str = Field(default="", max_length=100, description="节点名（空则自动生成）")
    params: Dict[str, Any] = Field(default_factory=dict, description="节点参数（JSON）")
    next_on_success: str = Field(
        default="", description=f"成功跳转到节点名；空或 {END_TARGET} = 到此结束"
    )
    next_on_fail: str = Field(
        default="", description=f"失败跳转到节点名；空或 {END_TARGET} = 中止（失败态）"
    )
