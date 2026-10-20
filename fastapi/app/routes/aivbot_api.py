"""
Aivbot-compatible HTTP API endpoints

These endpoints mimic the original Aivbot cloud server API so that
the Aivbot Android app can communicate with our local server.
"""

import logging
import os
import re
import shutil
import time
from datetime import datetime
from fastapi import APIRouter, Query, File, UploadFile, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
from ..config import settings

logger = logging.getLogger(__name__)

# 509 上传文件保存根目录：项目根目录/uploads
UPLOAD_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    settings.UPLOAD_DIR,
)

router = APIRouter(tags=["aivbot-compat"])


# ── Response models matching Aivbot cloud API ──────────


class AivbotResponse(BaseModel):
    code: int = 200
    message: str = "success"
    data: Optional[dict] = None


class RobotConfigData(BaseModel):
    openCallback: Optional[int] = 0
    callbackUrl: Optional[str] = None
    replyAll: Optional[int] = 0
    robotKeyCheck: Optional[int] = 0
    encryptType: Optional[int] = 0
    createTime: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────


@router.get("/ping")
async def test_connection():
    """Aivbot host connectivity test"""
    return {"code": 200, "message": "success", "data": "ok"}


@router.get("/robot/robotInfo/get")
async def get_robot_config(robotId: str = Query("", alias="robotId")):
    """
    Get robot configuration.
    Called by Aivbot app after saving robot ID.
    """
    from ..services import robot_service

    now = datetime.now().isoformat()
    robot = None
    if robotId:
        robot = await robot_service.get_robot(robotId)

    # 回复策略以服务器为准：replyAll 从设备绑定的策略组读取
    strategy = await robot_service.get_robot_strategy(robotId) if robotId else None
    reply_all = strategy.reply_all if strategy else (robot.reply_all if robot else 0)

    return {
        "code": 200,
        "message": "success",
        "data": {
            "openCallback": 0,
            "callbackUrl": None,
            "replyAll": reply_all,
            "robotKeyCheck": 0,
            "encryptType": 0,
            "createTime": robot.created_at if robot else now,
        },
    }


@router.post("/robot/robotInfo/update")
async def update_robot_config(payload: Optional[dict] = None):
    """
    Update robot configuration.
    Called by Aivbot app.

    Accepts JSON body: {"robotId": "...", "openCallback": 0, ...}
    - 回复策略（replyAll）已改为服务器端管理，忽略设备上报的此字段
    - 其它字段（如 openCallback/callbackUrl）：当前仅记录，不影响已实现的配置
    """
    from ..services import robot_service

    payload = payload or {}
    robot_id = payload.get("robotId") or ""

    if not robot_id:
        return {"code": 400, "message": "robotId is required"}

    # 设备可能尚未注册：懒注册后更新（validate_robot 会自动注册该 robot_id）
    if await robot_service.get_robot(robot_id) is None:
        await robot_service.validate_robot(robot_id)

    # 回复策略由服务器管理，忽略设备上报的 replyAll（防止设备缓存旧值覆盖管理员设置）
    if "replyAll" in payload:
        logger.info(
            f"robotInfo/update: {robot_id} ignored replyAll={payload['replyAll']} (server-managed)"
        )

    return {"code": 200, "message": "success"}


@router.get("/appUpdate/checkUpdate")
async def check_update():
    """
    Check for app updates (always returns no update).
    Called by Aivbot app on startup.
    """
    return {
        "code": 200,
        "message": "success",
        "data": None,
    }


@router.post("/fileUpload/upload")
async def file_upload(
    robotId: str = Query("", alias="robotId"),
    file: UploadFile = File(...),
):
    """
    File upload endpoint.

    Called by Aivbot app (command 509 获取本地文件) when pushing local files.
    保存文件到 uploads/{robotId}/ 下，并返回可访问地址与本地路径，方便核对。
    """
    robot_id = re.sub(r"[^A-Za-z0-9_.\-]", "_", (robotId or "").strip()) or "default"
    original_name = file.filename or f"upload_{int(time.time())}"
    safe_name = re.sub(r"[^\w.\-]", "_", original_name)

    robot_dir = os.path.join(UPLOAD_ROOT, robot_id)
    os.makedirs(robot_dir, exist_ok=True)

    dest_path = os.path.join(robot_dir, safe_name)
    # 同名文件防覆盖：追加毫秒时间戳
    if os.path.exists(dest_path):
        name, ext = os.path.splitext(safe_name)
        dest_path = os.path.join(robot_dir, f"{name}_{int(time.time() * 1000)}{ext}")

    try:
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        await file.close()

    size = os.path.getsize(dest_path)
    logger.info(
        f"文件上传成功: robotId={robot_id} name={os.path.basename(dest_path)} "
        f"size={size} path={dest_path}"
    )
    return {
        "code": 200,
        "message": "success",
        "data": {
            "url": f"/fileUpload/files/{robot_id}/{os.path.basename(dest_path)}",
            "localPath": dest_path.replace("\\", "/"),
            "fileName": os.path.basename(dest_path),
            "size": size,
        },
    }


@router.post("/imageUpload/upload")
async def image_upload(
    robotId: str = Query("", alias="robotId"),
    file: UploadFile = File(...),
):
    """
    图片消息上传接口。

    由 Aivbot 抓取聊天记录时调用，将图片保存到 uploads/{robotId}/ 目录，
    返回 upload 目录中的相对路径（data.path），随后随 WebSocket 消息回传给调度服务。
    """
    robot_id = re.sub(r"[^A-Za-z0-9_.\-]", "_", (robotId or "").strip()) or "default"
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        ext = ".png"
    filename = f"img_{int(time.time() * 1000)}{ext}"

    robot_dir = os.path.join(UPLOAD_ROOT, robot_id)
    os.makedirs(robot_dir, exist_ok=True)
    dest_path = os.path.join(robot_dir, filename)

    try:
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        await file.close()

    size = os.path.getsize(dest_path)
    # upload 目录中的相对路径（相对项目根目录）
    rel_path = os.path.join(settings.UPLOAD_DIR, robot_id, filename).replace("\\", "/")
    logger.info(f"图片上传成功: robotId={robot_id} path={rel_path} size={size}")
    return {
        "code": 200,
        "message": "success",
        "data": {
            "path": rel_path,
            "url": f"/fileUpload/files/{robot_id}/{filename}",
            "fileName": filename,
            "size": size,
        },
    }


@router.get("/fileUpload/files/{robot_id}/{filename}")
async def file_download(robot_id: str, filename: str):
    """
    下载已上传的文件。

    用于人工/脚本核对 509 是否真的把文件传上来了。
    """
    safe_name = os.path.basename(filename)
    dest_path = os.path.join(UPLOAD_ROOT, robot_id, safe_name)
    if not os.path.exists(dest_path):
        raise HTTPException(status_code=404, detail=f"File {filename} not found")
    return FileResponse(dest_path, filename=safe_name)


@router.get("/fileUpload/list")
async def file_list(robotId: str = Query("", alias="robotId")):
    """
    列出某 robotId 上传过的所有文件。

    用于快速确认 509 上传结果。
    """
    robot_id = re.sub(r"[^A-Za-z0-9_.\-]", "_", (robotId or "").strip()) or "default"
    robot_dir = os.path.join(UPLOAD_ROOT, robot_id)
    if not os.path.isdir(robot_dir):
        return {"code": 200, "message": "success", "data": {"files": []}}

    files = []
    for name in sorted(os.listdir(robot_dir)):
        fp = os.path.join(robot_dir, name)
        if os.path.isfile(fp):
            files.append(
                {
                    "fileName": name,
                    "size": os.path.getsize(fp),
                    "url": f"/fileUpload/files/{robot_id}/{name}",
                }
            )
    return {"code": 200, "message": "success", "data": {"files": files}}
