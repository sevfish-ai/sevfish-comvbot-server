#!/usr/bin/env python3
"""
Server startup script
"""
import uvicorn
from app.config import settings
from app.main import app


if __name__ == "__main__":
    # 强制单进程、禁用热重载：
    # 1) reload=False：WebSocket 连接注册表在进程内存中，热重载重启 worker 会静默清空注册表（设备误报离线）
    # 2) 直接传 app 对象而非字符串：避免 uvicorn 对 import string 的进程管理行为差异
    uvicorn.run(
        app,
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
        log_level="info"
    )
