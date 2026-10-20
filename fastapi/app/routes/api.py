"""
HTTP API routes
"""

import logging
import traceback
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ..services import aivbot_service, robot_service, agent_service
from ..models.agent import AgentCreateRequest, AgentBindRequest


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["api"])


class MessageRequest(BaseModel):
    """Message request model"""

    client_id: str
    received_name: str
    text: str
    room_type: int = 1


class ReplyAllRequest(BaseModel):
    """Reply strategy update request model"""

    reply_all: int  # -1=只读不回调 0=仅@回复 1=全部回调


class StatusResponse(BaseModel):
    """Status response model"""

    status: str
    data: dict


# ════════════════════════════════════════════
#  Robot Management APIs
# ════════════════════════════════════════════


@router.get("/robots")
async def list_robots():
    """
    List all registered robots with status

    Returns:
        List of all robot devices and statistics
    """
    try:
        robots = await robot_service.list_robots()
        stats = await robot_service.get_stats()
        return {
            "status": "success",
            "data": {**stats, "robots": [r.model_dump() for r in robots]},
        }
    except Exception as e:
        logger.error(f"list_robots error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/robot/{robot_id}")
async def get_robot(robot_id: str):
    """
    Get robot details by ID

    Args:
        robot_id: Robot identifier

    Returns:
        Robot details
    """
    robot = await robot_service.get_robot(robot_id)
    if robot is None:
        raise HTTPException(status_code=404, detail=f"Robot {robot_id} not found")

    return {"status": "success", "data": robot.model_dump()}


@router.post("/robot/{robot_id}/ban")
async def ban_robot(robot_id: str):
    """
    Ban a robot device (prevent it from connecting)

    Args:
        robot_id: Robot identifier to ban
    """
    success = await robot_service.ban_robot(robot_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Robot {robot_id} not found")

    return {"status": "success", "message": f"Robot {robot_id} has been banned"}


@router.post("/robot/{robot_id}/unban")
async def unban_robot(robot_id: str):
    """
    Unban a robot device

    Args:
        robot_id: Robot identifier to unban
    """
    success = await robot_service.unban_robot(robot_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Robot {robot_id} not found")

    return {"status": "success", "message": f"Robot {robot_id} has been unbanned"}


@router.delete("/robot/{robot_id}")
async def delete_robot(robot_id: str):
    """
    Delete a robot registration

    Args:
        robot_id: Robot identifier to delete
    """
    success = await robot_service.delete_robot(robot_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Robot {robot_id} not found")

    return {"status": "success", "message": f"Robot {robot_id} has been deleted"}


@router.post("/robot/{robot_id}/reply_all")
async def set_robot_reply_all(robot_id: str, request: ReplyAllRequest):
    """
    Set robot reply strategy (persisted server-side)

    Args:
        robot_id: Robot identifier
        request: reply_all = 0 (仅@回复) or 1 (全部回调)
    """
    try:
        success = await robot_service.update_reply_all(
            robot_id, request.reply_all
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not success:
        raise HTTPException(status_code=404, detail=f"Robot {robot_id} not found")

    return {
        "status": "success",
        "message": f"Robot {robot_id} reply_all set to {request.reply_all}",
    }


# ════════════════════════════════════════════
#  Message & Status APIs
# ════════════════════════════════════════════


@router.get("/status", response_model=StatusResponse)
async def get_status():
    """
    Get server status

    Returns:
        Status information including active connections and message count
    """
    ws_stats = aivbot_service.get_stats()
    robot_stats = await robot_service.get_stats()
    # 在线数以连接注册表为准（DB status 列会漂移，见 admin/test 页同款处理）
    connected = set(aivbot_service.active_connections)
    try:
        robots = await robot_service.list_robots()
        robot_stats["online"] = sum(
            1 for r in robots if r.status != "banned" and r.robot_id in connected
        )
        robot_stats["offline"] = max(
            robot_stats.get("total", 0) - robot_stats["online"] - robot_stats.get("banned", 0), 0
        )
    except Exception:
        pass

    return StatusResponse(status="success", data={**ws_stats, "robots": robot_stats})


@router.get("/clients")
async def list_clients():
    """
    List all connected clients

    Returns:
        List of client IDs
    """
    return {
        "status": "success",
        "data": {
            "count": len(aivbot_service.active_connections),
            "clients": list(aivbot_service.active_connections.keys()),
        },
    }


@router.post("/send")
async def send_message(request: MessageRequest):
    """
    Send message to a specific client

    Args:
        request: Message details

    Returns:
        Success status
    """
    success = await aivbot_service.send_reply(
        client_id=request.client_id,
        to_name=request.received_name,
        reply_text=request.text,
        room_type=request.room_type,
    )

    if success:
        return {"status": "success", "message": "Message sent successfully"}
    else:
        raise HTTPException(
            status_code=404,
            detail=f"Client {request.client_id} not found or send failed",
        )


@router.post("/broadcast")
async def broadcast_message(text: str):
    """
    Broadcast message to all connected clients

    Args:
        text: Message text to broadcast

    Returns:
        Broadcast result
    """
    message = {"type": "broadcast", "text": text}

    await aivbot_service.broadcast(message)

    return {
        "status": "success",
        "message": f"Broadcast sent to {len(aivbot_service.active_connections)} clients",
    }


# ════════════════════════════════════════════
#  Agent (智能体) APIs
# ════════════════════════════════════════════


@router.get("/agents")
async def list_agents():
    """List all agents"""
    try:
        agents = await agent_service.list_agents()
        # 隐藏完整 API Key，仅返回末尾 4 位
        data = []
        for a in agents:
            item = a.model_dump()
            item["api_key"] = (a.api_key[-4:] if a.api_key else "")
            data.append(item)
        return {"status": "success", "data": data}
    except Exception as e:
        logger.error(f"list_agents error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agents")
async def create_agent(request: AgentCreateRequest):
    """Create a new agent"""
    try:
        agent_id = await agent_service.create_agent(request)
        return {"status": "success", "message": "Agent created", "data": {"id": agent_id}}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"create_agent error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agent/{agent_id}")
async def get_agent(agent_id: int):
    """Get agent details by ID"""
    agent = await agent_service.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    item = agent.model_dump()
    item["api_key"] = (agent.api_key[-4:] if agent.api_key else "")
    return {"status": "success", "data": item}


@router.put("/agent/{agent_id}")
async def update_agent(agent_id: int, request: AgentCreateRequest):
    """Update agent (api_key empty = keep existing key)"""
    try:
        success = await agent_service.update_agent(agent_id, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"update_agent error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
    if not success:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    return {"status": "success", "message": f"Agent {agent_id} updated"}


@router.delete("/agent/{agent_id}")
async def delete_agent(agent_id: int):
    """Delete agent (unbind robots + remove conversations)"""
    success = await agent_service.delete_agent(agent_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    return {"status": "success", "message": f"Agent {agent_id} deleted"}


@router.post("/agent/{agent_id}/test")
async def test_agent(agent_id: int):
    """Test agent connection (calls the LLM once)"""
    agent = await agent_service.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    ok, result = await agent_service.test_connection(agent)
    return {
        "status": "success" if ok else "error",
        "data": {"ok": ok, "message": result},
    }


@router.post("/robot/{robot_id}/agent")
async def set_robot_agent(robot_id: str, request: AgentBindRequest):
    """Bind an agent to a robot (agent_id null/0 = unbind)"""
    if not await robot_service.get_robot(robot_id):
        raise HTTPException(status_code=404, detail=f"Robot {robot_id} not found")
    if request.agent_id and request.agent_id > 0:
        try:
            success = await agent_service.bind_agent(robot_id, request.agent_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        if not success:
            raise HTTPException(status_code=404, detail=f"Agent {request.agent_id} not found")
        return {"status": "success", "message": f"Robot {robot_id} bound to agent #{request.agent_id}"}
    success = await agent_service.unbind_agent(robot_id)
    return {"status": "success", "message": f"Robot {robot_id} agent unbound"}
