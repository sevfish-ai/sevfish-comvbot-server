"""
WebSocket routes
"""

import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from ..services import aivbot_service, robot_service


logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


@router.websocket("/webserver/wework/{client_id}")
async def aivbot_websocket(websocket: WebSocket, client_id: str):
    """
    Aivbot-compatible WebSocket endpoint

    Matches the Aivbot default path format:
        ws://host:port/webserver/wework/{robot_id}

    Args:
        websocket: WebSocket connection
        client_id: Robot ID / client identifier
    """
    # Validate robot (auto-register if unknown)
    is_valid, error_msg = await robot_service.validate_robot(client_id)
    if not is_valid:
        await websocket.close(code=4001, reason=error_msg)
        logger.warning(f"Connection rejected: {client_id} - {error_msg}")
        return

    # Accept connection
    await aivbot_service.connect(websocket, client_id)

    # Mark robot as online
    await robot_service.set_online(client_id)

    try:
        # Send welcome message
        await websocket.send_json(
            {
                "type": "welcome",
                "message": f"Connected as {client_id}",
                "timestamp": aivbot_service.get_stats()["timestamp"],
            }
        )

        # Message loop
        while True:
            data = await websocket.receive_json()
            # 自愈：设备仍在发消息说明连接活着，若该连接不在注册表则补注册
            conns = aivbot_service.active_connections.get(client_id, [])
            if websocket not in conns:
                conns.append(websocket)
                logger.warning(f"🔁 重新注册 {client_id}（连接自愈，设备实际在线）")
                try:
                    await robot_service.set_online(client_id)
                except Exception as e:
                    logger.warning(f"set_online failed for {client_id}: {e}")
            await aivbot_service.handle_message(data, client_id)

    except WebSocketDisconnect:
        logger.info(f"Client {client_id} disconnected")
        aivbot_service.disconnect(client_id, websocket)
        if client_id not in aivbot_service.active_connections:
            await robot_service.set_offline(client_id)

    except Exception as e:
        logger.error(f"WebSocket error for {client_id}: {e}")
        aivbot_service.disconnect(client_id, websocket)
        if client_id not in aivbot_service.active_connections:
            await robot_service.set_offline(client_id)

    finally:
        aivbot_service.disconnect(client_id, websocket)
        if client_id not in aivbot_service.active_connections:
            await robot_service.set_offline(client_id)


@router.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """
    Legacy WebSocket endpoint (generic path)

    Args:
        websocket: WebSocket connection
        client_id: Client identifier
    """
    # Accept connection without validation (for testing)
    await aivbot_service.connect(websocket, client_id)
    # 与正式端点对齐：这个端点以前不回写 DB 状态，导致"注册表有连接、DB 却是离线"
    try:
        await robot_service.set_online(client_id)
    except Exception as e:
        logger.warning(f"set_online failed for {client_id}: {e}")

    try:
        await websocket.send_json(
            {
                "type": "welcome",
                "message": f"Connected as {client_id}",
                "timestamp": aivbot_service.get_stats()["timestamp"],
            }
        )

        while True:
            data = await websocket.receive_json()
            # 自愈：设备仍在发消息说明连接活着，若该连接不在注册表则补注册
            conns = aivbot_service.active_connections.get(client_id, [])
            if websocket not in conns:
                conns.append(websocket)
                logger.warning(f"🔁 重新注册 {client_id}（连接自愈，设备实际在线）")
                try:
                    await robot_service.set_online(client_id)
                except Exception as e:
                    logger.warning(f"set_online failed for {client_id}: {e}")
            await aivbot_service.handle_message(data, client_id)

    except WebSocketDisconnect:
        logger.info(f"Client {client_id} disconnected")
        aivbot_service.disconnect(client_id, websocket)
        if client_id not in aivbot_service.active_connections:
            await robot_service.set_offline(client_id)

    except Exception as e:
        logger.error(f"WebSocket error for {client_id}: {e}")
        aivbot_service.disconnect(client_id, websocket)
        if client_id not in aivbot_service.active_connections:
            await robot_service.set_offline(client_id)

    finally:
        aivbot_service.disconnect(client_id, websocket)
        if client_id not in aivbot_service.active_connections:
            await robot_service.set_offline(client_id)


@router.websocket("/ws")
async def websocket_endpoint_auto(websocket: WebSocket):
    """
    WebSocket endpoint with auto-generated client ID
    """
    import uuid

    client_id = f"client_{uuid.uuid4().hex[:8]}"

    await websocket_endpoint(websocket, client_id)
