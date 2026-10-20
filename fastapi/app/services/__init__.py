"""
Services module
"""
# 注意：aivbot_service 必须从模块导入同一个实例（模块底部已有单例），
# 若在此处重新 AivbotService() 会创建第二个实例，导致：
#   - websocket.py 用包实例注册连接（from ..services import aivbot_service）
#   - admin.py 用模块实例查注册表（from ..services.aivbot_service import aivbot_service）
#   两者是不同的对象 → 连接在 A 里、下发在 B 里 → 设备永远误报离线
from .aivbot_service import AivbotService, aivbot_service
from .robot_service import RobotService, robot_service
from .group_service import GroupService, group_service
from .knowledge_service import KnowledgeService, knowledge_service
from .agent_service import AgentService, agent_service
from .table_service import TableService, table_service
from .workflow_service import WorkflowService, workflow_service
from .workflow_engine import workflow_engine
from .task_service import TaskService, task_service
from .skill_service import SkillService, skill_service
from .ext_user_service import ExtUserService, ext_user_service
from .memory_service import MemoryService, memory_service
# 组织配置智能体（kind=provisioner）的工具实现：纯函数模块，由 agent_service 按需调用
from . import provision_service  # noqa: E402

robot_service = robot_service
group_service = group_service
knowledge_service = knowledge_service
agent_service = agent_service
table_service = table_service
workflow_service = workflow_service
task_service = task_service
skill_service = skill_service
ext_user_service = ext_user_service
memory_service = memory_service
from .memory_cleanup_service import memory_cleanup_service  # noqa: E402  （依赖 memory_service 单例）

__all__ = [
    "aivbot_service",
    "robot_service",
    "group_service",
    "knowledge_service",
    "agent_service",
    "table_service",
    "workflow_service",
    "workflow_engine",
    "task_service",
    "skill_service",
    "ext_user_service",
    "memory_service",
    "memory_cleanup_service",
    "provision_service",
    "AivbotService",
    "RobotService",
    "GroupService",
    "KnowledgeService",
    "AgentService",
    "TableService",
    "WorkflowService",
    "TaskService",
    "SkillService",
    "ExtUserService",
    "MemoryService",
]
