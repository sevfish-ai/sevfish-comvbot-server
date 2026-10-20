"""
Application configuration
"""
import os
from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings"""

    # Server settings
    APP_NAME: str = "SevFish"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True

    # Server configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8765
    # 必须关闭热重载：WebSocket 连接注册表在 worker 进程内存中，
    # --reload 模式下代码变更会重启 worker，导致所有已注册连接被静默清空（设备误报离线）。
    # 改代码后请手动重启服务。
    RELOAD: bool = False

    # WebSocket settings
    WS_HEARTBEAT_INTERVAL: int = 10  # seconds
    WS_HEARTBEAT_TIMEOUT: int = 10   # seconds
    WS_MAX_CONNECTIONS: int = 100

    # CORS settings
    CORS_ORIGINS: List[str] = ["*"]
    CORS_ALLOW_CREDENTIALS: bool = True
    CORS_ALLOW_METHODS: List[str] = ["*"]
    CORS_ALLOW_HEADERS: List[str] = ["*"]

    # ── 外网客户访问（/ext）────────────────────────────────
    # 隧道共享密钥：由 frpc 通过 requestHeaders.set 注入 X-Tunnel-Secret 头。
    # 留空 = /ext 整体关闭（默认安全）；配置后只有带正确密钥头的隧道请求能进 /ext。
    EXT_TUNNEL_SECRET: str = ""
    # 本地预览：允许**内网直连**访问 /ext（免隧道密钥头），用于没有云机时先看效果/内网调试。
    # 默认 False。开启时仅放行私网来源，且启动横幅会告警；生产环境必须保持 False。
    EXT_LOCAL_PREVIEW: bool = False
    # 会话有效期（小时）。到期即失效；客户有活动时会自动续期（见 ext_session_middleware）。
    EXT_SESSION_HOURS: int = 72
    # 会话 cookie 的 Secure 标志：生产走 HTTPS 隧道必须 True；
    # 本地用 http://127.0.0.1 联调时可临时置 false（浏览器不接受 http 上的 Secure cookie）。
    EXT_COOKIE_SECURE: bool = True
    # 官网地址：对外控制台顶栏/页脚的「官方网站」链接指向它。备案通过后改成正式域名。
    SITE_URL: str = "https://www.sevfishai.com"
    # 运营主体：客户控制台页脚与官网版权一致
    COMPANY_NAME: str = "示例科技有限公司"

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # Upload (509 本地文件上传保存目录，相对项目根目录)
    UPLOAD_DIR: str = "uploads"

    # MySQL
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "root"
    MYSQL_PASSWORD: str = "123456"
    MYSQL_DATABASE: str = "aivbot"
    MYSQL_POOL_MIN_SIZE: int = 2
    MYSQL_POOL_MAX_SIZE: int = 10

    class Config:
        env_file = ".env"
        case_sensitive = True


# Create settings instance
settings = Settings()