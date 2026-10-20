"""
API routes
"""
from .websocket import router as websocket_router
from .api import router as api_router
from .admin import router as admin_router
from .aivbot_api import router as aivbot_router
from .test import router as test_router
from .groups import router as groups_router
from .agents import router as agents_router
from .knowledge import router as knowledge_router
from .tables import router as tables_router
from .workflows import router as workflows_router
from .tasks import router as tasks_router
from .skills import router as skills_router
from .memory import router as memory_router
from .docs_zh import router as docs_zh_router
from .provision import router as provision_router
from .ext import ext_session_middleware, ext_tunnel_guard, router as ext_router

__all__ = [
    "websocket_router",
    "api_router",
    "admin_router",
    "aivbot_router",
    "test_router",
    "groups_router",
    "agents_router",
    "knowledge_router",
    "tables_router",
    "workflows_router",
    "tasks_router",
    "skills_router",
    "memory_router",
    "docs_zh_router",
    "provision_router",
    "ext_router",
    "ext_tunnel_guard",
    "ext_session_middleware",
]