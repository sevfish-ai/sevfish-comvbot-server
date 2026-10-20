"""
WebSocket 连接与基础 HTTP 接口测试
"""
import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture(scope="session")
def client():
    """
    创建测试客户端（with 块以触发 lifespan：MySQL pool / 表初始化）。

    session 级：lifespan 只启停一次。若每个测试各建一个 TestClient，
    task_service.start() 的后台任务在 shutdown 后清理不干净，会污染
    pytest 事件循环导致后续测试卡死。
    """
    with TestClient(app) as c:
        yield c


def test_root_redirects_to_orgs(client):
    """Root endpoint redirects to org selection (303)"""
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/orgs"


def test_health_check(client):
    """Test health check endpoint"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_api_status(client):
    """Test API status endpoint"""
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "active_connections" in data["data"]


def test_api_clients(client):
    """Test list clients endpoint"""
    response = client.get("/api/clients")
    assert response.status_code == 200
    data = response.json()
    assert "clients" in data["data"]


def test_websocket_connection(client):
    """Test WebSocket connection（/ws 自动分配 client_id；复用 fixture 避免重复触发 lifespan）"""
    from app.models.message import MessageType

    with client.websocket_connect("/ws") as websocket:
        # Receive welcome message
        data = websocket.receive_json()
        assert data["type"] == "welcome"

        # Send heartbeat
        websocket.send_json({"type": MessageType.HEART_BEAT})

        # Receive heartbeat response
        response = websocket.receive_json()
        assert response["type"] == MessageType.HEART_BEAT
