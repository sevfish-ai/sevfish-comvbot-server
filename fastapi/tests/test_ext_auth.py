"""外网客户入口（/ext）三道门的最小自检：密码、会话签名、隧道密钥头。

不连数据库 —— 这里只测纯逻辑与中间件/路由行为。
DB 相关端点由 tests/manual_ext_smoke.py 在真实库上手工验证（见部署清单）。

回归背景：/ext 是唯一对公网暴露的面，它的"门"一旦失效就等于把内网后台送出去，
所以三道门各自都要有能独立失败的断言。
"""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.routes.ext import ext_tunnel_guard, router as ext_router, tunnel_check  # noqa: E402
from app.services.ext_user_service import (  # noqa: E402
    hash_password,
    make_session,
    parse_session,
    session_key,
    verify_password,
)

# ── 门 0：密码 ────────────────────────────────────────


def test_password_roundtrip():
    salt, h = hash_password("s3cret")
    assert verify_password("s3cret", salt, h)
    assert not verify_password("s3cret2", salt, h)


def test_password_salt_is_random():
    s1, h1 = hash_password("same")
    s2, h2 = hash_password("same")
    assert s1 != s2 and h1 != h2
    assert verify_password("same", s1, h1) and verify_password("same", s2, h2)


def test_password_empty_or_broken_rejected():
    assert not verify_password("x", "", "abc")
    assert not verify_password("x", "salt", "")
    assert not verify_password("x", "salt", "not-a-hash")


# ── 门 2：会话 ────────────────────────────────────────


def test_session_roundtrip():
    key = session_key("tunnel-secret")
    data = parse_session(make_session(1001, 7, key), key)
    assert data["u"] == 1001 and data["o"] == 7


def test_session_rejects_tamper_and_wrong_key():
    key = session_key("tunnel-secret")
    token = make_session(1001, 7, key)
    body, sig = token.rsplit(".", 1)
    assert parse_session(f"{body}.{sig[:-1]}x", key) is None
    assert parse_session(token, session_key("other-secret")) is None
    assert parse_session("garbage", key) is None
    assert parse_session("", key) is None
    assert parse_session("a.b.c", key) is None


def test_session_expired():
    key = session_key("s")
    assert parse_session(make_session(1, 2, key, ttl=-1), key) is None


# ── 会话有效期与惰性续期（纯函数）─────────────────────


def test_session_ttl_follows_config(monkeypatch):
    from app.services.ext_user_service import session_ttl

    monkeypatch.setattr(settings, "EXT_SESSION_HOURS", 72, raising=False)
    assert session_ttl() == 72 * 3600
    monkeypatch.setattr(settings, "EXT_SESSION_HOURS", 2, raising=False)
    assert session_ttl() == 2 * 3600
    # 非法/极端值不能变成"永不过期"或负数
    monkeypatch.setattr(settings, "EXT_SESSION_HOURS", 0, raising=False)
    assert session_ttl() == 3600
    monkeypatch.setattr(settings, "EXT_SESSION_HOURS", "bad", raising=False)
    assert session_ttl() == 12 * 3600


def test_needs_refresh_only_after_half_lived():
    from app.routes.ext import needs_refresh

    ttl = 72 * 3600
    now = 1_000_000
    # 刚签发：剩余=TTL → 不续期（避免每个响应都塞 Set-Cookie）
    assert needs_refresh({"e": now + ttl}, ttl, now) is False
    # 用掉一半多：该续期了
    assert needs_refresh({"e": now + ttl // 2 - 1}, ttl, now) is True
    # 已过期：也算该续期（调用方在此之前已判定会话有效）
    assert needs_refresh({"e": now - 1}, ttl, now) is True
    assert needs_refresh(None, ttl, now) is True


# ── 门 1：隧道密钥头（纯函数）─────────────────────────


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(settings, "EXT_TUNNEL_SECRET", "s3cret", raising=False)
    return "s3cret"


def test_tunnel_rules(secret):
    assert tunnel_check("/ext/ping", None) != ""          # 内网直连 /ext → 拒
    assert tunnel_check("/admin/robots", None) == ""      # 内网直连 /admin → 放行
    assert tunnel_check("/ext/ping", secret) == ""        # 隧道 + 正确密钥 → 放行
    assert tunnel_check("/ext/ping", "wrong") != ""       # 密钥错 → 拒
    assert tunnel_check("/admin/robots", secret) != ""    # 隧道想摸 /admin → 拒
    assert tunnel_check("/webserver/wework/123456", secret) != ""  # 隧道摸 WS 路径 → 拒


def test_tunnel_local_preview(secret):
    """本地预览：只在显式开启 + 私网来源 + 无密钥头时放行。"""
    assert tunnel_check("/ext/ping", None, "192.168.31.97", True) == ""
    assert tunnel_check("/ext/ping", None, "127.0.0.1", True) == ""
    assert tunnel_check("/ext/ping", None, "1.2.3.4", True) != ""        # 公网来源 → 拒
    assert tunnel_check("/ext/ping", None, "192.168.31.97", False) != ""  # 未开启 → 拒
    # 带密钥头时仍走隧道规则：预览开关不能成为 /admin 的旁路
    assert tunnel_check("/admin/robots", "wrong", "192.168.31.97", True) != ""
    assert tunnel_check("/admin/robots", secret, "192.168.31.97", True) != ""


def test_tunnel_disabled_without_secret(monkeypatch):
    monkeypatch.setattr(settings, "EXT_TUNNEL_SECRET", "", raising=False)
    assert tunnel_check("/ext/ping", "anything") != ""
    assert tunnel_check("/admin/x", None) == ""


# ── 门 1 + 2：HTTP 行为 ───────────────────────────────


def _client() -> TestClient:
    app = FastAPI()
    app.middleware("http")(ext_tunnel_guard)
    app.include_router(ext_router)
    return TestClient(app)


def test_ext_blocked_without_tunnel_header(secret):
    c = _client()
    assert c.get("/ext/ping").status_code == 403
    assert c.get("/ext/login").status_code == 403


def test_ext_allows_tunnel_header(secret):
    c = _client()
    r = c.get("/ext/ping", headers={"X-Tunnel-Secret": secret})
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_admin_path_blocked_even_with_valid_header(secret):
    c = _client()
    assert c.get("/admin/robots", headers={"X-Tunnel-Secret": secret}).status_code == 403


def test_login_page_renders(secret):
    c = _client()
    r = c.get("/ext/login", headers={"X-Tunnel-Secret": secret})
    assert r.status_code == 200 and "客户登录" in r.text
    # 内网产品的"企"字标记不该出现在客户面上（顶栏曾长期带着它，很出戏）
    assert "企" not in r.text
    # 品牌标记：统一走全 app 的 /favicon.svg（不再是内联锁图标），语义不变：中性、无内网痕迹
    assert 'class="x-mark"' in r.text and "/favicon.svg" in r.text
    assert 'rel="icon"' in r.text  # 标签页图标


def test_home_redirects_to_login_without_session(secret):
    c = _client()
    r = c.get("/ext/", headers={"X-Tunnel-Secret": secret}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ext/login"


def test_old_cookie_does_not_loop_login_page(secret, monkeypatch):
    """回归：登录页与 require_user 必须用同一套会话判断。

    密码指纹上线后，浏览器里遗留的旧 cookie（无指纹）曾造成
    /ext/login → /ext/ → /ext/login 无限 303，客户直接打不开控制台。
    """
    # 注意：app.services.ext_user_service 这个名字在包命名空间里被单例实例覆盖了
    # （见 AGENTS.md），所以用 importlib 取真模块，而不是 from app.services import ...
    from importlib import import_module

    from app.services.ext_user_service import make_session, session_key

    svc = import_module("app.services.ext_user_service")

    async def fake_get(uid):
        return {"id": 1, "org_id": 1, "username": "u", "enabled": True, "pwd_fp": "fp1"}

    monkeypatch.setattr(svc.ext_user_service, "get", fake_get, raising=False)
    key = session_key(secret)
    c = _client()

    # 旧格式会话（没有指纹）：登录页要能正常渲染，而不是把用户弹去首页再弹回来
    c.cookies.set("ext_session", make_session(1, 1, key))
    r = c.get("/ext/login", headers={"X-Tunnel-Secret": secret}, follow_redirects=False)
    assert r.status_code == 200 and "客户登录" in r.text
    # 首页仍然拒绝它（失效是真的失效，不是放宽）
    r = c.get("/ext/", headers={"X-Tunnel-Secret": secret}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ext/login"

    # 指纹正确的会话：登录页才跳首页
    c.cookies.set("ext_session", make_session(1, 1, key, fp="fp1"))
    r = c.get("/ext/login", headers={"X-Tunnel-Secret": secret}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ext/"


def test_bad_session_cookie_redirects_to_login(secret):
    c = _client()
    c.cookies.set("ext_session", "forged.token")
    r = c.get(
        "/ext/devices", headers={"X-Tunnel-Secret": secret}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/ext/login"
