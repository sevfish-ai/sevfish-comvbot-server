"""
FastAPI application entry point
"""

import logging
import os
import aiomysql
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from .config import settings
from .brand import FAVICON_SVG
from .routes import (
    websocket_router,
    api_router,
    admin_router,
    aivbot_router,
    test_router,
    groups_router,
    agents_router,
    knowledge_router,
    tables_router,
    workflows_router,
    tasks_router,
    skills_router,
    memory_router,
    docs_zh_router,
    provision_router,
    ext_router,
    ext_session_middleware,
    ext_tunnel_guard,
)
from .services import (
    aivbot_service,
    robot_service,
    group_service,
    agent_service,
    knowledge_service,
    table_service,
    workflow_service,
    task_service,
    skill_service,
    memory_service,
    memory_cleanup_service,
    ext_user_service,
    provision_service,
)
from .services.aivbot_service import install_loop_exception_handler
from .layout import page, esc


# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL), format=settings.LOG_FORMAT
)

logger = logging.getLogger(__name__)


# ── 访问日志噪音过滤 ────────────────────────────────────
# Chrome DevTools 会探测这个路径做工作区自动发现；开着 DevTools 时每次刷新都留一条 404，
# 与业务无关。用过滤器丢掉，而不是加一个假路由（加路由照样会打日志，只是变成 200）。
_QUIET_ACCESS_PATHS = (
    "/.well-known/appspecific/com.chrome.devtools.json",
)


class _QuietPathFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(p in msg for p in _QUIET_ACCESS_PATHS)


# 挂在 logger 上（不是 handler），uvicorn 的 dictConfig 不会清掉它
logging.getLogger("uvicorn.access").addFilter(_QuietPathFilter())


# Create FastAPI application
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="""
    SevFish 服务端

    WebSocket server for communication with the Android client.

    ## Features

    - **WebSocket Communication**: Real-time bidirectional communication
    - **HTTP API**: RESTful API for message sending, robot registration and management
    - **Admin Dashboard**: Web-based device management
    - **Robot Registration**: Register and manage multiple devices

    ## Usage

    1. Register a robot via API or admin dashboard
    2. Connect via WebSocket: `ws://localhost:8765/webserver/wework/{robot_id}`
    3. Send/receive JSON messages
    4. Monitor status via HTTP API

    """,
    docs_url="/docs",
    redoc_url="/redoc",
)


# 本地静态资源（CodeMirror 等）
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=settings.CORS_ALLOW_METHODS,
    allow_headers=settings.CORS_ALLOW_HEADERS,
)

# 外网客户入口：会话惰性续期（内层）→ 隧道密钥头（外层，先拦掉非隧道请求）。
# 注册顺序即嵌套顺序：后注册的在更外层。
app.middleware("http")(ext_session_middleware)
app.middleware("http")(ext_tunnel_guard)


# Register routers
app.include_router(websocket_router)
app.include_router(api_router)
app.include_router(admin_router)
app.include_router(aivbot_router)
app.include_router(test_router)
app.include_router(groups_router)
app.include_router(agents_router)
app.include_router(knowledge_router)
app.include_router(tables_router)
app.include_router(workflows_router)
app.include_router(tasks_router)
app.include_router(skills_router)
app.include_router(memory_router)
app.include_router(docs_zh_router)
app.include_router(provision_router)
app.include_router(ext_router)


@app.on_event("startup")
async def startup_event():
    """Application startup"""
    # 未认领的异步异常统一走 logging（含堆栈），别只在 stderr 留一行默认告警
    install_loop_exception_handler()
    # ── Initialize MySQL pool ─────────────────────────────
    try:
        pool = await aiomysql.create_pool(
            host=settings.MYSQL_HOST,
            port=settings.MYSQL_PORT,
            user=settings.MYSQL_USER,
            password=settings.MYSQL_PASSWORD,
            db=settings.MYSQL_DATABASE,
            minsize=settings.MYSQL_POOL_MIN_SIZE,
            maxsize=settings.MYSQL_POOL_MAX_SIZE,
            autocommit=True,  # 确保每次操作立即生效，避免读取到旧数据
            charset="utf8mb4",
        )
        robot_service.set_pool(pool)
        await robot_service.ensure_table()
        await robot_service.reset_all_online()
        group_service.set_pool(pool)
        await group_service.ensure_table()
        agent_service.set_pool(pool)
        await agent_service.ensure_table()
        knowledge_service.set_pool(pool)
        await knowledge_service.ensure_table()
        table_service.set_pool(pool)
        await table_service.ensure_table()
        workflow_service.set_pool(pool)
        await workflow_service.ensure_table()
        task_service.set_pool(pool)
        await task_service.ensure_table()
        skill_service.set_pool(pool)
        await skill_service.ensure_table()
        memory_service.set_pool(pool)
        await memory_service.ensure_table()
        ext_user_service.set_pool(pool)
        await ext_user_service.ensure_table()

        # ── 自增 ID 从 1000 起步 ─────────────────────────────
        # 智能体/工作流/任务等 ID 若从 1 开始过短，LLM 收到 run_workflow_{id} 等指令时易误解。
        # 动态扫描所有自增值 <1000 的表统一抬到 1000：老表已有短 ID 的行保持不动，新行从 1000 起；
        # 新库/老库/未来新增表一次覆盖，幂等（自增已 ≥1000 的表会被 WHERE 跳过）。
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """SELECT TABLE_NAME FROM information_schema.TABLES
                           WHERE TABLE_SCHEMA = DATABASE()
                             AND AUTO_INCREMENT IS NOT NULL
                             AND AUTO_INCREMENT < 1000"""
                    )
                    low_tables = [r[0] for r in await cur.fetchall()]
                    for tbl in low_tables:
                        try:
                            await cur.execute(
                                f"ALTER TABLE `{tbl}` AUTO_INCREMENT = 1000"
                            )
                        except Exception as e:
                            logger.warning(f"抬升 {tbl} 自增起点失败: {e}")
            if low_tables:
                logger.info(
                    f"✅ 自增 ID 起点调至 1000: {', '.join(sorted(low_tables))}"
                )
        except Exception as e:
            logger.warning(f"自增起点抬升跳过（不影响启动）: {e}")

        app.state.db_pool = pool
        logger.info("✅ MySQL pool initialized")

        # ── 组织配置智能体（系统级，kind=provisioner）就位 ─────
        # 它不属任何组织，工具集是 provision_*（直接建库级资源）；LLM 与提示词在
        # 「组织选择 → AI 配置组织」页单独配置，提示词只在首次创建时写入。
        try:
            pv = await agent_service.ensure_provisioner(
                provision_service.PROVISIONER_NAME,
                provision_service.PROVISIONER_DESC,
                provision_service.DEFAULT_PROMPT,
            )
            logger.info(
                f"🤖 组织配置智能体 #{pv.id}「{pv.name}」就位"
                f"（模型：{pv.model or '未配置'}）"
            )
        except Exception as e:
            logger.warning(f"组织配置智能体初始化跳过: {e}")

        await task_service.start()  # 启动定时调度 + 条件触发引擎
        await memory_cleanup_service.start()  # 启动记忆清理调度（到期的记忆集自动整理）
    except Exception as e:
        logger.critical(f"❌ Failed to connect to MySQL: {e}")
        raise

    # ── Print banner ──────────────────────────────────────
    logger.info("=" * 60)
    logger.info(f"🚀 {settings.APP_NAME} v{settings.APP_VERSION}")
    logger.info("=" * 60)
    logger.info(
        f"📍 WebSocket: ws://{settings.HOST}:{settings.PORT}/webserver/wework/{{robot_id}}"
    )
    logger.info(
        f"📍 WebSocket legacy: ws://{settings.HOST}:{settings.PORT}/ws/{{client_id}}"
    )
    logger.info(f"📍 API: http://{settings.HOST}:{settings.PORT}/api")
    logger.info(f"📍 Admin: http://{settings.HOST}:{settings.PORT}/admin")
    logger.info(f"📍 Agents: http://{settings.HOST}:{settings.PORT}/admin/agents")
    logger.info(f"📍 Knowledge: http://{settings.HOST}:{settings.PORT}/admin/knowledge")
    logger.info(f"📍 Tables: http://{settings.HOST}:{settings.PORT}/admin/tables")
    logger.info(f"📍 Workflows: http://{settings.HOST}:{settings.PORT}/admin/workflows")
    logger.info(f"📍 Groups: http://{settings.HOST}:{settings.PORT}/groups")
    logger.info(f"📍 Docs: http://{settings.HOST}:{settings.PORT}/docs")
    logger.info(
        f"📍 MySQL: {settings.MYSQL_USER}@{settings.MYSQL_HOST}:{settings.MYSQL_PORT}/{settings.MYSQL_DATABASE}"
    )
    if settings.EXT_TUNNEL_SECRET:
        logger.info("📍 外网客户入口: /ext（已启用，仅接受带 X-Tunnel-Secret 头的隧道请求）")
    elif settings.EXT_LOCAL_PREVIEW:
        logger.warning(
            "📍 外网客户入口: /ext（⚠️ 本地预览模式：内网直连免密钥头，生产必须关闭 EXT_LOCAL_PREVIEW）"
        )
    else:
        logger.info("📍 外网客户入口: /ext（未启用；配置 EXT_TUNNEL_SECRET 后开放）")
    logger.info("=" * 60)


@app.on_event("shutdown")
async def shutdown_event():
    """Application shutdown"""
    await task_service.stop()  # 停止任务调度/条件轮询
    await memory_cleanup_service.stop()
    pool = getattr(app.state, "db_pool", None)
    if pool:
        pool.close()
        await pool.wait_closed()
        logger.info("👋 MySQL pool closed")
    logger.info("👋 Shutting down server...")


@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    """全站图标（管理端 /admin 与客户控制台 /ext 共用同一份）。"""
    return Response(content=FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=604800"})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    """老浏览器默认请求 /favicon.ico，重定向到 SVG，避免访问日志刷 404。"""
    return RedirectResponse("/favicon.svg", status_code=308)


@app.get("/", response_class=HTMLResponse)
async def root():
    """入口即组织选择：访问根路径直接进入组织选择页"""
    return RedirectResponse(url="/admin/orgs", status_code=303)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy"}
