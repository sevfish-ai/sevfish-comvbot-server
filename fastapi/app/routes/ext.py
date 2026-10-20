"""
外网客户访问（/ext/*）—— 服务端渲染的只读为主界面

定位：给外网客户（浏览器）用的窄接口面。**不暴露任何内网管理能力**：
  · 只能看到本组织（org_groups）授权范围内的表格 / 设备 / 智能体 / 知识库
  · 只能改设备的推送开关、设备的智能体绑定、本组织智能体的非密钥配置
  · 表格只读；LLM API Key 不出现在页面上，也不能通过本路由写入

三道门（缺一不可）：
  1. 隧道密钥头 X-Tunnel-Secret（ext_tunnel_guard，见 main.py 注册）—— 只有隧道来的请求能进 /ext
  2. 登录会话（HMAC 签名 cookie，Path=/ext）
  3. 组织归属校验（每个资源都过 load_scope，杜绝越权）

设计取舍：全部服务端渲染 + 表单 POST，不引入前端框架、不新增依赖。
"""

import csv
import hmac
import io
import ipaddress
import json
import logging
import os
import time
from collections import deque
from datetime import date, timedelta
from typing import Dict, Optional, Set
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from ..brand import FAVICON_SVG
from ..config import settings
from ..layout import esc
from ..models.agent import AgentCreateRequest
from ..services import (
    agent_service,
    group_service,
    knowledge_service,
    memory_service,
    robot_service,
    table_service,
)
from ..services.memory_service import (
    CLEANUP_FREQ_CHOICES,
    cleanup_freq_label,
    CUSTOMER_NAME_MAX,
    ITEM_MAX_CHARS,
    ITEMS_PER_CUSTOMER,
    LIMITS as MEMORY_LIMITS,
    SET_MAX_CUSTOMERS,
    SET_MAX_ITEMS,
)
from ..services.ext_user_service import (
    SESSION_COOKIE,
    ext_user_service,
    make_session,
    parse_session,
    session_key,
    session_ttl,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ext", tags=["ext"])

# 组织授权的资源类型（与 org_group_members.member_type 一致）
_SCOPE_KINDS = ("table", "device", "agent", "knowledge", "memory")


# ── 门 1：隧道密钥头 ──────────────────────────────────


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _is_private_ip(host: str) -> bool:
    """私网/回环地址判定（frpc 在本地，隧道请求也会显示为 127.0.0.1，
    所以只有'没有密钥头'的私网请求才算内网直连预览）。"""
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def is_preview_request(request: Request) -> bool:
    """本地预览请求：显式开启 + 无密钥头 + 来源是私网。"""
    return (
        bool(settings.EXT_LOCAL_PREVIEW)
        and request.headers.get("x-tunnel-secret") is None
        and _is_private_ip(_client_ip(request))
    )


def tunnel_check(
    path: str,
    header_secret: Optional[str],
    client_ip: str = "",
    allow_local: bool = False,
) -> str:
    """返回 "" 放行，否则返回拒绝原因。纯函数，便于单测。

    规则（依据 frpc 的 requestHeaders.set 会给所有经隧道的请求注入该头）：
      · 带头的请求 = 来自隧道 → 密钥必须对，且路径必须在 /ext 下
      · 不带头的请求 = 直连 → /ext 默认拒绝；仅在 EXT_LOCAL_PREVIEW 且来源为私网时放行
    这样即使 frp 的 locations 配错把 /admin 转了出去，那一半也进不来。
    """
    secret = settings.EXT_TUNNEL_SECRET or ""
    if header_secret is None:
        if not path.startswith("/ext"):
            return ""
        if allow_local and _is_private_ip(client_ip):
            return ""  # 本地预览：内网直连（见 EXT_LOCAL_PREVIEW）
        return "非隧道来源不允许访问 /ext"
    if not secret:
        return "外网访问未启用（未配置 EXT_TUNNEL_SECRET）"
    if not hmac.compare_digest(header_secret, secret):
        return "隧道密钥不匹配"
    if not path.startswith("/ext"):
        return "隧道只放行 /ext"
    return ""


async def ext_tunnel_guard(request: Request, call_next):
    reason = tunnel_check(
        request.url.path,
        request.headers.get("x-tunnel-secret"),
        client_ip=_client_ip(request),
        allow_local=bool(settings.EXT_LOCAL_PREVIEW),
    )
    if reason:
        logger.warning(f"🚫 拦截 {request.url.path}: {reason}")
        return PlainTextResponse("403 Forbidden", status_code=403)
    return await call_next(request)


# ── 门 2：登录会话 ────────────────────────────────────


def _login_redirect() -> HTTPException:
    return HTTPException(status_code=303, headers={"Location": "/ext/login"})


def needs_refresh(data: dict, ttl: Optional[int] = None, now: Optional[int] = None) -> bool:
    """剩余有效期不足一半就该续期。

    不是每个请求都换 cookie（那会让每个响应都带 Set-Cookie、白白增加开销），
    而是等到用掉一半时才换一次；换过之后又是完整的 TTL。
    """
    ttl = session_ttl() if ttl is None else ttl
    now = int(time.time()) if now is None else now
    return int((data or {}).get("e", 0)) - now < ttl // 2


def _cookie_secure(request: Request) -> bool:
    """生产走 HTTPS 必须 Secure；本地预览是 http，加了浏览器就不存 cookie。"""
    return settings.EXT_COOKIE_SECURE and not is_preview_request(request)


def _issue_session(resp, user: dict, secure: bool):
    """签发会话 cookie：带上密码指纹，改密后旧 cookie 自动失效。"""
    resp.set_cookie(
        SESSION_COOKIE,
        make_session(
            user["id"],
            user["org_id"],
            session_key(settings.EXT_TUNNEL_SECRET),
            fp=user.get("pwd_fp", ""),
        ),
        max_age=session_ttl(),
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/ext",
    )


async def current_user(request: Request) -> Optional[dict]:
    """从 cookie 解析出当前登录用户；任何一项不合格都返回 None。

    **所有判断只在这里做一次**：签名/有效期 → 账号存在且启用 → 组织未变 → 密码指纹一致。
    登录页判断"是否已登录"必须走同一个函数：否则两处标准不一致，
    就会出现 /ext/login → /ext/ → /ext/login 的重定向死循环。
    """
    token = request.cookies.get(SESSION_COOKIE)
    data = parse_session(token, session_key(settings.EXT_TUNNEL_SECRET)) if token else None
    if not data:
        return None
    user = await ext_user_service.get(data["u"])
    if not user or not user["enabled"] or int(user["org_id"]) != int(data["o"]):
        return None
    # 指纹不匹配 = 签发之后密码被改过，旧会话一律作废
    if data.get("f", "") != user.get("pwd_fp", ""):
        logger.info(f"↩︎ 会话失效（密码已变更）: {user['username']}")
        return None
    request.state.ext_session = data  # 供中间件判断是否需要续期
    return user


async def resolved_user(request: Request) -> Optional[dict]:
    """取当前登录用户：优先用中间件已解析的结果，避免一个请求查两次库。"""
    if getattr(request.state, "ext_user_resolved", False):
        return getattr(request.state, "ext_user", None)
    return await current_user(request)


async def ext_session_middleware(request: Request, call_next):
    """会话惰性续期：客户一有活动就延长有效期。

    顺手把解析结果缓存到 request.state，后续 require_user 直接复用（不重复查库）。
    """
    if request.url.path.startswith("/ext"):
        user = await current_user(request)
        request.state.ext_user = user
        request.state.ext_user_resolved = True
        if user and needs_refresh(getattr(request.state, "ext_session", None)):
            response = await call_next(request)
            _issue_session(response, user, _cookie_secure(request))
            return response
    return await call_next(request)


async def require_user(request: Request) -> dict:
    """会话校验依赖。未登录/已停用/组织变更/密码已改 → 回登录页。"""
    user = await resolved_user(request)
    if not user:
        raise _login_redirect()
    return user


# 登录失败限流（进程内存；frp 下所有请求都可能来自同一 IP，所以按 用户名+IP 计数）
# ponytail: 只防在线暴力猜；多实例部署会失效（本方案单进程），要抗分布式爆破时再上 Redis
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW = 300
_login_fails: Dict[str, deque] = {}


def _throttled(key: str) -> bool:
    q = _login_fails.get(key)
    if not q:
        return False
    now = time.time()
    while q and now - q[0] > _LOGIN_WINDOW:
        q.popleft()
    return len(q) >= _LOGIN_MAX_FAILS


def _clear_fail(key: str):
    _login_fails.pop(key, None)


def _record_fail(key: str):
    if len(_login_fails) > 1000:  # 粗暴兜底，避免长期运行无限增长
        _login_fails.clear()
    _login_fails.setdefault(key, deque()).append(time.time())


# ── 门 3：组织归属 ────────────────────────────────────


async def load_scope(org_id: int, user_id: int = 0) -> Dict[str, Set[str]]:
    """账号实际能看到的资源 id 集合（统一转 str 比较）。

    两级授权：组织决定「拥有什么」，账号范围决定「能看其中哪些」。
      · 账号从未配过范围 → 跟随组织（全可见，兼容老账号）
      · 配过（有任何记录）→ **完全以账号范围为准，且必须仍在组织内**；
        某类别没有记录 = 该类别看不见（失败关闭，避免"只给了 1 张表却漏出全部设备"）
    """
    org_scope: Dict[str, Set[str]] = {}
    for kind in _SCOPE_KINDS:
        org_scope[kind] = {
            str(x) for x in await group_service.get_group_org_members(org_id, kind)
        }
    if not user_id:
        return org_scope

    user_scope = await ext_user_service.get_scope(user_id)
    if not user_scope:  # 未配置过 → 跟随组织
        return org_scope
    # 与组织取交集：陈旧的范围记录不能越过组织边界
    return {kind: org_scope[kind] & set(user_scope.get(kind, [])) for kind in _SCOPE_KINDS}


def in_scope(scope: Dict[str, Set[str]], kind: str, member_id) -> bool:
    return str(member_id) in scope.get(kind, set())


# ── 对外控制台样式（独立外链，浏览器可缓存；改样式后把链接里的 v 号 +1）──

_CSS_VER = "26"

EXT_CSS = """
:root{
  /* ── 与官网同一套设计令牌 ── */
  --bg:#F5F9FF; --surface:#fff; --tint:#F0F9FE;
  --border:#E3EBF7; --border-2:#C9D8EE;
  --text:#0F172A; --text-2:#4A5A70; --text-3:#5E6C80;
  --primary:#155EEF; --primary-h:#0E4FD8; --primary-bg:#EAF1FF;
  --cyan:#00B2ED; --violet:#5740FF;
  --ink:#080C14; --ink-2:#0B1730; --ink-3:#1A1140;
  --brand:linear-gradient(120deg,#0E6FE0 0%,#155EEF 46%,#4F35F0 100%);
  --ok:#0F766E; --ok-bg:#E6FBF5; --off:#64748B; --off-bg:#EEF2F8;
  --warn:#B45309; --warn-bg:#FEF3C7; --red:#DC2626; --red-bg:#FEE2E2;
  --r:16px; --r-sm:10px;
  --sh:0 1px 2px rgba(21,94,239,.05),0 14px 34px -24px rgba(21,94,239,.42);
  --sh-lg:0 2px 6px rgba(21,94,239,.08),0 26px 54px -28px rgba(87,64,255,.55);
  --font:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB','Microsoft YaHei',Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,'Cascadia Code',monospace;
}
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:var(--font);background:var(--bg);color:var(--text);
  font-size:14px;line-height:1.65;-webkit-font-smoothing:antialiased;
  -webkit-text-size-adjust:100%;-webkit-tap-highlight-color:transparent;
  /* 页脚吸底：内容不足一屏时页脚仍贴在视口底部（登录页是单子元素，不受影响）*/
  min-height:100vh;min-height:100dvh;display:flex;flex-direction:column;
}
a{color:var(--primary);text-decoration:none}
h1,h2,h3{font-weight:650;letter-spacing:-.01em}
code{font-family:var(--mono);font-size:12.5px;background:var(--off-bg);padding:1px 5px;border-radius:5px}
::selection{background:rgba(21,94,239,.18)}

/* ── 顶栏：深色，与官网 header / 页脚同一基调 ── */
.x-top{
  position:sticky;top:0;z-index:20;color:#fff;
  background:
    radial-gradient(620px 260px at 88% -40%,rgba(87,64,255,.42),transparent 62%),
    linear-gradient(150deg,#080C14,#0B1730 60%,#161036);
  border-bottom:1px solid rgba(255,255,255,.1);
  box-shadow:0 10px 30px -22px rgba(8,12,20,.9);
  padding:0 env(safe-area-inset-right,0) 0 env(safe-area-inset-left,0);
}
.x-top-in{
  max-width:1120px;margin:0 auto;padding:14px 20px 0;
  display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;
}
.x-brand{display:flex;align-items:center;gap:10px;min-width:0;flex-wrap:wrap}
.x-brand-ico{width:24px;height:24px;flex:none;border-radius:8px;display:block;
  box-shadow:0 4px 14px -4px rgba(21,94,239,.9)}
.x-brand-t{font-size:16px;font-weight:650;color:#fff;line-height:1.3;letter-spacing:-.01em}
.x-brand-s{
  font-size:11.5px;font-weight:600;color:#CBE6FF;background:rgba(255,255,255,.1);
  border:1px solid rgba(255,255,255,.2);padding:2px 9px;border-radius:999px;white-space:nowrap;
}
.x-user{display:flex;align-items:center;gap:9px;font-size:13px;color:rgba(255,255,255,.75)}
.x-avatar{
  width:29px;height:29px;flex:none;border-radius:50%;display:grid;place-items:center;
  font-size:12.5px;font-weight:650;color:#fff;background:var(--brand);
  box-shadow:0 4px 12px -4px rgba(21,94,239,.9);
}
/* 账号菜单：<details> 让"点击/触摸展开"在所有浏览器原生可用（手机上 hover 不存在），
   桌面（真有指针）再额外支持悬停展开 —— 用 pointer:fine 限定，
   避免触屏设备上"第一次点击只触发 hover、菜单闪一下"的经典坑。 */
.x-menu{position:relative}
.x-menu-trigger{
  display:flex;align-items:center;gap:8px;
  height:38px;padding:3px 11px 3px 4px;border-radius:999px;cursor:pointer;
  border:1px solid transparent;list-style:none;user-select:none;transition:.15s;
}
.x-menu-trigger::-webkit-details-marker{display:none}
.x-menu-trigger:hover{background:rgba(255,255,255,.1);border-color:rgba(255,255,255,.2)}
.x-menu[open]>.x-menu-trigger{background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.24)}
.x-caret{font-size:10px;color:rgba(255,255,255,.6);transition:transform .15s}
.x-menu[open] .x-caret{transform:rotate(180deg)}
.x-menu-panel{
  display:none;position:absolute;right:0;top:calc(100% + 8px);z-index:30;
  min-width:176px;padding:6px;background:#fff;
  border:1px solid var(--border);border-radius:12px;
  box-shadow:0 20px 44px -18px rgba(8,12,20,.5);
}
.x-menu[open]>.x-menu-panel{display:block}
.x-menu-panel a{
  display:flex;align-items:center;gap:9px;padding:9px 11px;border-radius:8px;
  font-size:13.5px;color:var(--text);white-space:nowrap;transition:.12s;
}
.x-menu-panel a:hover{background:var(--primary-bg);color:var(--primary)}
.x-menu-panel a.danger:hover{background:var(--red-bg);color:var(--red)}
.x-menu-panel a span{width:16px;text-align:center;font-size:13px}
@media(hover:hover) and (pointer:fine){
  .x-menu:hover>.x-menu-panel{display:block}
}
.x-nav{
  max-width:1120px;margin:0 auto;padding:12px 20px 13px;
  display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;
}
.x-nav::-webkit-scrollbar{display:none}
.x-tab{
  flex:none;padding:7px 14px;border-radius:999px;font-size:13.5px;font-weight:550;
  color:rgba(255,255,255,.72);white-space:nowrap;transition:.15s;
  border:1px solid transparent;
}
.x-tab:hover{color:#fff;background:rgba(255,255,255,.1)}
.x-tab.on{color:#fff;background:var(--brand);border-color:transparent;font-weight:600;
  box-shadow:0 8px 20px -10px rgba(21,94,239,.95)}

/* ── 主体 ── */
.x-main{flex:1 0 auto;width:100%;max-width:1120px;margin:0 auto;padding:26px 20px 56px}
.x-hd{
  position:relative;overflow:hidden;margin-bottom:18px;padding:20px 22px;
  border:1px solid var(--border);border-radius:var(--r);
  background:linear-gradient(135deg,rgba(0,178,237,.08),rgba(87,64,255,.08)),#fff;
  box-shadow:var(--sh);
}
.x-hd::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--brand)}
.x-hd-t{font-size:22px;line-height:1.3}
.x-hd-s{font-size:13px;color:var(--text-2);margin-top:4px}
.x-alert{
  padding:12px 15px;border-radius:var(--r-sm);font-size:13.5px;margin-bottom:16px;
  display:flex;align-items:center;gap:8px;border:1px solid transparent;
}
.x-alert-ok{background:var(--ok-bg);color:var(--ok);border-color:#BBF7D0}
.x-alert-err{background:var(--red-bg);color:var(--red);border-color:#FECACA}

/* ── 卡片 ── */
.x-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);box-shadow:var(--sh);padding:22px;margin-bottom:18px}
.x-card-hd{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:4px}
.x-card-t{font-size:16px}
.x-card-s{font-size:12.5px;color:var(--text-3);line-height:1.7;margin-bottom:14px}
.x-card-s:last-child{margin-bottom:0}

/* ── 统计卡 ── */
.x-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));gap:14px;margin-bottom:18px}
.x-stat{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--r);box-shadow:var(--sh);
  padding:18px;display:flex;align-items:center;gap:14px;transition:.15s;
}
a.x-stat:hover{transform:translateY(-2px);box-shadow:var(--sh-lg);border-color:var(--border-2)}
.x-stat-ic{
  width:44px;height:44px;flex:none;border-radius:12px;display:grid;place-items:center;font-size:20px;
  background:var(--brand);color:#fff;box-shadow:0 10px 22px -12px rgba(21,94,239,.9);
}
.x-stat:nth-child(2) .x-stat-ic{background:linear-gradient(135deg,#0A8FD8,#0E6FE0)}
.x-stat:nth-child(3) .x-stat-ic{background:linear-gradient(135deg,#155EEF,#4F35F0)}
.x-stat:nth-child(4) .x-stat-ic{background:linear-gradient(135deg,#4F35F0,#7C50F5)}
.x-stat-n{
  font-size:24px;font-weight:680;line-height:1.2;
  background:var(--brand);-webkit-background-clip:text;background-clip:text;color:transparent;
}
.x-stat-l{font-size:12.5px;color:var(--text-2)}

/* ── 表格 ── */
.x-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:var(--r-sm)}
.x-table{width:100%;border-collapse:collapse;font-size:13.5px}
.x-table th{
  text-align:left;font-size:11.5px;font-weight:600;color:var(--text-3);letter-spacing:.06em;
  padding:10px 12px;background:var(--tint);border-bottom:1px solid var(--border);
  white-space:nowrap;text-transform:uppercase;
}
.x-table td{padding:12px;border-bottom:1px solid var(--border);vertical-align:middle}
.x-table tr:last-child td{border-bottom:none}
.x-table tbody tr:hover td,.x-table tr:hover td{background:linear-gradient(90deg,rgba(0,178,237,.05),rgba(87,64,255,.05))}
.x-table .x-sub{font-size:11.5px;color:var(--text-3);font-family:var(--mono)}

/* ── 徽章 ── */
.x-pill{
  display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:999px;
  font-size:11.5px;font-weight:600;white-space:nowrap;border:1px solid transparent;
}
.x-pill::before{content:'';width:5px;height:5px;border-radius:50%;background:currentColor;opacity:.8}
.x-pill-ok{background:var(--ok-bg);color:var(--ok);border-color:#BBF7D0}
.x-pill-off{background:var(--off-bg);color:var(--off);border-color:var(--border)}
.x-pill-info{background:var(--primary-bg);color:var(--primary);border-color:#CFE0FF}
.x-pill-warn{background:var(--warn-bg);color:var(--warn);border-color:#FDE68A}

/* ── 按钮 ── */
.x-btn{
  -webkit-appearance:none;appearance:none;
  display:inline-flex;align-items:center;justify-content:center;gap:6px;
  height:38px;padding:0 16px;border:1px solid transparent;border-radius:var(--r-sm);
  font-family:inherit;font-size:13.5px;font-weight:600;cursor:pointer;transition:.15s;
  background:var(--off-bg);color:var(--text);white-space:nowrap;
}
.x-btn:active{transform:translateY(1px)}
.x-btn-primary{background:var(--brand);color:#fff;box-shadow:0 10px 24px -12px rgba(21,94,239,.85)}
.x-btn-primary:hover{color:#fff;box-shadow:0 14px 30px -12px rgba(21,94,239,.95)}
.x-btn-outline{background:#fff;border-color:var(--border-2);color:var(--text)}
.x-btn-outline:hover{border-color:var(--primary);color:var(--primary);background:var(--primary-bg)}
.x-btn-ghost{background:transparent;color:var(--text-2)}
.x-btn-ghost:hover{background:var(--off-bg);color:var(--text)}
.x-btn-danger{background:transparent;border-color:#FECACA;color:var(--red)}
.x-btn-danger:hover{background:var(--red-bg)}
.x-btn-sm{height:31px;padding:0 12px;font-size:12.5px;border-radius:8px}
.x-btn-block{width:100%}

/* ── 表单 ── */
.x-row{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}
.x-field{display:flex;flex-direction:column;gap:6px;flex:1;min-width:160px}
.x-label{font-size:12.5px;font-weight:550;color:var(--text-2)}
.x-input,.x-select,.x-textarea{
  -webkit-appearance:none;appearance:none;
  width:100%;font-family:inherit;font-size:13.5px;color:var(--text);
  background:var(--surface);border:1px solid var(--border-2);border-radius:var(--r-sm);
  padding:0 12px;height:40px;transition:.15s;
}
.x-textarea{height:auto;padding:10px 12px;line-height:1.7;resize:vertical}
.x-input:focus,.x-select:focus,.x-textarea:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-bg)}
.x-select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%235E6C80' d='M6 8.5 1.5 4h9z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;padding-right:32px}
.x-hint{font-size:12.5px;color:var(--text-3)}

/* ── 设备卡 ── */
.x-devs{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.x-dev{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);box-shadow:var(--sh);padding:18px}
.x-dev-hd{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
.x-dev-n{font-size:14.5px;font-weight:620;line-height:1.35}
.x-dev-id{font-size:11.5px;color:var(--text-3);font-family:var(--mono)}
.x-dev-meta{display:flex;flex-direction:column;gap:10px;border-top:1px solid var(--border);padding-top:12px}
.x-dev-line{display:flex;align-items:center;justify-content:space-between;gap:10px}
.x-dev-line .x-label{margin:0;flex:none}

/* 开关（纯 CSS，无 JS）*/
.x-switch{position:relative;display:inline-block;width:46px;height:26px;flex:none}
.x-switch input{opacity:0;width:0;height:0}
.x-switch span{
  position:absolute;inset:0;background:var(--border-2);border-radius:999px;cursor:pointer;transition:.2s;
}
.x-switch span::after{
  content:'';position:absolute;left:3px;top:3px;width:20px;height:20px;border-radius:50%;
  background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.2);transition:.2s;
}
.x-switch input:checked + span{background:var(--brand)}
.x-switch input:checked + span::after{transform:translateX(20px)}
.x-switch input:focus-visible + span{box-shadow:0 0 0 3px var(--primary-bg)}

.x-guide{display:grid;grid-template-columns:repeat(2,1fr);gap:10px 24px;margin-top:12px}
.x-guide>div{display:flex;flex-direction:column;gap:2px;font-size:13.5px}
.x-guide b{font-weight:600}

.x-key{width:180px;color:var(--text-3);font-size:12.5px;font-weight:550}

.x-val{min-width:0}

.x-empty{padding:34px 10px;text-align:center;color:var(--text-3);font-size:13.5px}

/* ── 页脚 ── */
.x-foot{
  margin-top:auto;color:#fff;padding:28px 0 calc(20px + env(safe-area-inset-bottom,0));
  background:radial-gradient(560px 260px at 88% 0%,rgba(87,64,255,.34),transparent 62%),linear-gradient(160deg,#080C14,#0A1428 62%,#120C2E);
}
.x-foot-in{
  max-width:1120px;margin:0 auto;padding:0 20px;
  display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
  font-size:12.5px;color:rgba(255,255,255,.6);
}
.x-foot a{color:rgba(255,255,255,.82)}
.x-foot a:hover{color:#fff}
.x-foot-links{display:flex;gap:16px;flex-wrap:wrap}

/* ── 登录 ── */
.x-login{
  min-height:100vh;display:grid;place-items:center;padding:24px;color:#fff;
  background:
    radial-gradient(760px 420px at 12% -10%,rgba(0,178,237,.34),transparent 62%),
    radial-gradient(720px 420px at 88% 6%,rgba(87,64,255,.42),transparent 64%),
    linear-gradient(158deg,#080C14,#0B1730 56%,#1A1140);
}
.x-login-card{
  width:100%;max-width:412px;background:#fff;border:1px solid var(--border);border-radius:20px;
  box-shadow:0 40px 90px -40px rgba(0,0,0,.85);padding:34px 30px;color:var(--text);
}
.x-login-brand{display:flex;flex-direction:column;align-items:center;gap:12px;margin-bottom:22px;text-align:center}
.x-mark{
  width:54px;height:54px;flex:none;display:block;border-radius:16px;
  box-shadow:0 16px 34px -14px rgba(21,94,239,.75);
}
.x-mark svg{display:block}
.x-login-t{font-size:20px}
.x-login-s{font-size:12.5px;color:var(--text-3)}
.x-login .x-field{margin-bottom:14px}
.x-login .x-input{height:46px}
.x-login .x-btn{height:46px;font-size:14.5px}
.x-login-foot{margin-top:18px;text-align:center;font-size:12.5px;color:var(--text-3)}
.x-login-foot a{color:var(--primary)}

/* ── 窄屏（手机）── */
@media(max-width:820px){
  .x-top-in{padding:12px 14px 0}
  .x-nav{padding:10px 14px 12px}
  .x-tab{padding:8px 13px;font-size:13px}
  .x-brand-t{font-size:15px}
  .x-user{gap:7px;font-size:12.5px}
  .x-menu-trigger{height:40px}
  .x-menu-panel{min-width:168px}
  .x-menu-panel a{padding:12px;font-size:14px}
  .x-main{padding:18px 14px 44px}
  .x-hd{padding:16px 16px 16px 18px}
  .x-hd-t{font-size:19px}
  .x-card{padding:16px;border-radius:13px}
  .x-stats{grid-template-columns:repeat(2,1fr);gap:10px}
  .x-stat{padding:14px;gap:10px;flex-direction:column;align-items:flex-start}
  .x-stat-ic{width:38px;height:38px;font-size:17px}
  .x-stat-n{font-size:20px}
  .x-devs{grid-template-columns:1fr}
  .x-row{flex-direction:column;align-items:stretch;gap:12px}
  .x-field{min-width:0}
  .x-btn{height:42px}
  .x-btn-sm{height:34px}
  .x-table th,.x-table td{padding:10px 12px}
  /* 数据表格：窄屏改成"每条记录一张卡片"，彻底不需要左右滑动。
     表头隐藏，字段名由 td 的 data-label 提供（没有表头也看得懂）。 */
  .x-table.stack{white-space:normal}
  .x-table.stack thead{display:none}
  .x-table.stack tr{
    display:block;background:var(--surface);border:1px solid var(--border);
    border-radius:var(--r-sm);padding:10px 12px;margin-bottom:10px;box-shadow:var(--sh);
  }
  .x-table.stack tr.empty-row{border:none;padding:0;box-shadow:none}
  .x-table.stack td{
    display:flex;gap:12px;align-items:baseline;justify-content:space-between;
    border:none;padding:4px 0;text-align:left;white-space:normal;
  }
  .x-table.stack td::before{
    content:attr(data-label);flex:none;min-width:72px;
    color:var(--text-3);font-size:12.5px;font-weight:550;
  }
  /* 值里带副标题时：字段名在左，"标题 + 副标题"整体在右并上下堆叠。 */
  .x-table.stack td>.x-val{text-align:right}
  .x-table.stack td[data-label=""]{justify-content:flex-end;padding-top:8px}
  .x-table.stack td[data-label=""]::before{display:none}
  /* 键值表（只读配置、知识库元信息）本身就是 标签:值 结构，允许换行即可 */
  .x-table.kv{white-space:normal}
  .x-table.kv .x-key{width:112px}
  .x-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
  .x-login-card{padding:26px 20px}
  .x-guide{grid-template-columns:1fr;gap:10px}
  .x-foot-in{flex-direction:column;align-items:flex-start;gap:10px}
}
@media(max-width:400px){
  .x-uname{display:none}   /* 只留头像首字母 + 箭头，右上角不换行 */
}
@media (prefers-reduced-motion:reduce){
  *{transition:none!important;animation:none!important}
}
"""


@router.get("/static/ext.css")
async def ext_css():
    """对外控制台样式表。外链而非内嵌：多页浏览只下载一次（改样式记得 +_CSS_VER）。"""
    return Response(
        content=EXT_CSS,
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/static/favicon.svg")
async def ext_favicon():
    """控制台图标/logo。必须挂在 /ext 下：隧道只转发 /ext*，根路径的 /favicon.svg 归官网。"""
    return Response(
        content=FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=604800"},
    )


# ── 页面骨架（外网面：无内网链接、无侧边栏）──────────


_NAV = [
    ("/ext/", "首页", "home"),
    ("/ext/devices", "设备", "devices"),
    ("/ext/tables", "表格", "tables"),
    ("/ext/agents", "智能体", "agents"),
    ("/ext/knowledge", "知识库", "knowledge"),
    ("/ext/memory", "长期记忆", "memory"),
]


def _page(
    user: dict,
    title: str,
    heading: str,
    body: str,
    msg: str = "",
    status_code: int = 200,
    subtitle: str = "",
    active: str = "",
) -> HTMLResponse:
    msg_html = ""
    if msg:
        cls = "x-alert-ok" if msg.startswith("✅") else "x-alert-err"
        msg_html = f'<div class="x-alert {cls}">{esc(msg)}</div>'
    nav = "".join(
        f'<a class="x-tab{" on" if key == active else ""}" href="{url}">{label}</a>'
        for url, label, key in _NAV
    )
    nav += f'<a class="x-tab" href="{settings.SITE_URL}" target="_blank" rel="noopener">官方网站 ↗</a>'
    sub_html = f'<div class="x-hd-s">{esc(subtitle)}</div>' if subtitle else ""
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<title>{esc(title)} · SevFish</title>
<link rel="icon" href="/ext/static/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/ext/static/ext.css?v={_CSS_VER}">
</head>
<body>
<header class="x-top">
  <div class="x-top-in">
    <div class="x-brand">
      <img class="x-brand-ico" src="/ext/static/favicon.svg" width="24" height="24" alt="">
      <span class="x-brand-t">{esc(user.get("org_name") or "客户控制台")}</span>
      <span class="x-brand-s">SevFish</span>
    </div>
    <div class="x-user">
      <details class="x-menu">
        <summary class="x-menu-trigger" title="账号">
          <span class="x-avatar">{esc((user["username"] or "?")[:1].upper())}</span>
          <span class="x-uname">{esc(user["username"])}</span>
          <span class="x-caret">▾</span>
        </summary>
        <div class="x-menu-panel">
          <a href="{settings.SITE_URL}" target="_blank" rel="noopener"><span>🌐</span>返回官网</a>
          <a href="/ext/password"><span>🔑</span>修改密码</a>
          <a href="/ext/logout" class="danger"><span>⏻</span>退出登录</a>
        </div>
      </details>
    </div>
  </div>
  <nav class="x-nav">{nav}</nav>
</header>
<main class="x-main">
  <div class="x-hd"><h1 class="x-hd-t">{esc(heading)}</h1>{sub_html}</div>
  {msg_html}
  {body}
</main>
<footer class="x-foot">
  <div class="x-foot-in">
    <div>© {settings.COMPANY_NAME} · SevFish（SevFish AI）· 本服务用于企业自身的客户服务与效率提升，请遵守企业微信平台规范与相关法律法规。</div>
    <div class="x-foot-links">
      <a href="{settings.SITE_URL}" target="_blank" rel="noopener">官方网站</a>
    </div>
  </div>
</footer>
<script>
/* 账号菜单：<details> 点外部不会自动收起，这里补上（手机上尤其需要）。
   注意本页是 f-string，JS 的花括号要写成 {{{{ }}}} */
document.addEventListener('click', e => {{{{
  if (!e.target.closest('.x-menu'))
    document.querySelectorAll('.x-menu[open]').forEach(d => d.open = false);
}}}});
document.addEventListener('keydown', e => {{{{
  if (e.key === 'Escape')
    document.querySelectorAll('.x-menu[open]').forEach(d => d.open = false);
}}}});
</script>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=status_code)


def _login_page(msg: str = "", status_code: int = 200) -> HTMLResponse:
    msg_html = f'<div class="x-alert x-alert-err">{esc(msg)}</div>' if msg else ""
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<title>客户登录 · SevFish</title>
<link rel="icon" href="/ext/static/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/ext/static/ext.css?v={_CSS_VER}">
</head>
<body>
<div class="x-login">
  <div class="x-login-card">
    <div class="x-login-brand">
      <img class="x-mark" src="/ext/static/favicon.svg" width="54" height="54" alt="SevFish">
      <div>
        <div class="x-login-t">SevFish</div>
        <div class="x-login-s">客户控制台 · 请使用管理员分配的账号登录</div>
      </div>
    </div>
    {msg_html}
    <form method="POST" action="/ext/login">
      <div class="x-field"><label class="x-label">账号</label>
        <input class="x-input" name="username" autocomplete="username" required autofocus></div>
      <div class="x-field"><label class="x-label">密码</label>
        <input class="x-input" type="password" name="password" autocomplete="current-password" required></div>
      <button class="x-btn x-btn-primary x-btn-block">登 录</button>
    </form>
    <div class="x-login-foot"><a href="{settings.SITE_URL}">← 返回官方网站</a></div>
  </div>
</div>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=status_code)


async def _user_with_org(user: dict) -> dict:
    """补上组织名，页面头部要显示。"""
    org = await group_service.get_org_group(int(user["org_id"]))
    user = dict(user)
    user["org_name"] = (org or {}).get("name", f"组织 {user['org_id']}")
    return user


# 客户上传文档的准入白名单（与内网后台 accept 一致，但这里是服务端强校验）
_ALLOWED_DOC_EXT = {".txt", ".md", ".csv", ".json", ".log", ".pdf", ".docx"}
_MAX_UPLOAD_MB = 20  # ponytail: 硬编码够用；要按客户调额度时挪到配置项


# 重定向里只放 ASCII 码：Location 头必须 latin-1 可编码，中文/emoji 直接塞进去会 500
_MSG = {
    "ok": "✅ 已保存",
    "updated": "✅ 已更新",
    "fail": "❌ 操作失败",
    "invalid": "❌ 保存失败（检查取值是否合法）",
    "uploaded": "✅ 文档已上传并切分",
    "deleted": "✅ 文档已删除",
    "badext": "❌ 不支持的文件类型（仅支持 txt/md/csv/json/log/pdf/docx）",
    "toobig": f"❌ 文件超过 {_MAX_UPLOAD_MB}MB 上限",
    "empty": "❌ 文件为空或未解析到文本",
    "cloud": "❌ 云知识库的文档在平台侧维护，此处不能上传或删除",
    "nodoc": "❌ 文档不存在或不属于该知识库",
    "failed": "❌ 上传失败",
    "pwchanged": "✅ 密码已修改，其他设备上的登录已失效",
    "pwshort": "❌ 新密码至少 8 位",
    "pwmismatch": "❌ 两次输入的新密码不一致",
    "pwsame": "❌ 新密码不能与当前密码相同",
    "pwwrong": "❌ 当前密码不正确",
    "memok": "✅ 已添加记忆",
    "memupd": "✅ 记忆已更新",
    "memdel": "✅ 记忆已删除",
    "memdup": "❌ 这条记忆已存在（内容重复）",
    "memempty": "❌ 记忆内容不能为空",
    "memlong": "❌ 单条记忆太长（人工写入上限 500 字）",
    "memfull": "❌ 记忆集已满，请先清理旧记忆",
    "memcust": "❌ 客户不属于该记忆集",
    "memcustok": "✅ 客户已添加（记忆可以以后再补）",
    "memitem": "❌ 记忆不存在或不属于该记忆集",
    "memname": f"❌ 客户名不能为空且不超过 {CUSTOMER_NAME_MAX} 字",
    "memcustfull": f"❌ 这套记忆的客户数已达上限（{SET_MAX_CUSTOMERS} 位），请先清理或让管理员处理",
}


def _msg_text(code: str) -> str:
    return _MSG.get(code or "", "")


_KB_TYPE = {"local": "本地文档库", "bailian": "阿里云百炼", "http": "自定义 HTTP 接口"}

def _DENY(text: str) -> str:
    """403/404 用的提示卡（不暴露任何额外信息）"""
    return f'<div class="x-card"><div class="x-empty">🚫 {esc(text)}</div></div>'


def _badge(on: bool, text: str = "") -> str:
    """启用/停用徽章"""
    cls = "x-pill-ok" if on else "x-pill-off"
    label = text or ("启用" if on else "停用")
    return f'<span class="x-pill {cls}">{label}</span>'


def _push_switch(robot_id: str, auto_reply: bool, nxt: str = "devices") -> str:
    """LLM 推送开关：纯 CSS 开关 + onchange 提交。

    状态放在隐藏域（checkbox 不带 name），否则"未勾选就不提交"会被后端当成开启。
    nxt 决定提交后回哪个页面（白名单值，不做任意跳转）。
    """
    return f"""
        <form method="POST" action="/ext/device/{esc(robot_id)}/auto_reply">
          <input type="hidden" name="enabled" value="{1 if auto_reply else 0}">
          <input type="hidden" name="next" value="{esc(nxt)}">
          <label class="x-switch" title="关闭后该设备不再自动回复">
            <input type="checkbox" {'checked' if auto_reply else ''}
                   onchange="this.form.enabled.value = this.checked ? '1' : '0'; this.form.submit()">
            <span></span>
          </label>
        </form>"""


# 提交后允许回到的页面（白名单，避免开放重定向）
_NEXT_URL = {"devices": "/ext/devices", "home": "/ext/"}


def _next_url(nxt: str) -> str:
    return _NEXT_URL.get(nxt or "", "/ext/devices")


def _status_pill(status) -> str:
    """设备状态徽章（str-Enum 混入，取 value 更稳）"""
    val = str(getattr(status, "value", status))
    if val == "online":
        return '<span class="x-pill x-pill-ok">在线</span>'
    if val == "banned":
        return '<span class="x-pill x-pill-warn">已封禁</span>'
    return '<span class="x-pill x-pill-off">离线</span>'


# ── 隧道自检 ─────────────────────────────────────────


@router.get("/ping")
async def ext_ping():
    """部署验证用：隧道打通且密钥头正确时返回 ok。"""
    return {"status": "ok", "service": "ext"}


# ── 登录 / 登出 ──────────────────────────────────────


@router.get("/login")
async def login_page(request: Request):
    # 只有"首页确实会认"的会话才跳转，避免与 require_user 的标准不一致导致重定向死循环
    if await resolved_user(request):
        return RedirectResponse(url="/ext/", status_code=303)
    return _login_page()


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    ip = request.client.host if request.client else "-"
    key = f"{ip}|{username.strip()}"
    if _throttled(key):
        logger.warning(f"🚫 登录限流: {username}")
        return _login_page("尝试次数过多，请 5 分钟后再试", status_code=429)

    user = await ext_user_service.authenticate(username, password)
    if not user:
        _record_fail(key)
        logger.warning(f"❌ 外网登录失败: {username}")
        return _login_page("账号或密码错误", status_code=401)

    resp = RedirectResponse(url="/ext/", status_code=303)
    _issue_session(resp, user, _cookie_secure(request))
    logger.info(f"✅ 外网登录成功: {username} → 组织 {user['org_id']}")
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/ext/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/ext")
    return resp


# ── 修改密码 ─────────────────────────────────────────


def _password_page(user: dict, m: str = "", status_code: int = 200) -> HTMLResponse:
    body = f"""
    <div class="x-card" style="max-width:520px">
      <h2 class="x-card-t">修改密码</h2>
      <p class="x-card-s">为了确认是你本人操作，需要先输入当前密码。修改成功后，
      其他设备上用旧密码登录的会话会立即失效，本机保持登录。</p>
      <form method="POST" action="/ext/password">
        <div class="x-field"><label class="x-label">当前密码</label>
          <input class="x-input" type="password" name="old_password" autocomplete="current-password" required></div>
        <div class="x-field" style="margin-top:14px"><label class="x-label">新密码（至少 8 位）</label>
          <input class="x-input" type="password" name="new_password" minlength="8"
                 autocomplete="new-password" required></div>
        <div class="x-field" style="margin-top:14px"><label class="x-label">确认新密码</label>
          <input class="x-input" type="password" name="confirm" minlength="8"
                 autocomplete="new-password" required></div>
        <div style="margin-top:18px"><button class="x-btn x-btn-primary">保存新密码</button></div>
      </form>
    </div>"""
    return _page(user, "修改密码", "修改密码", body, msg=_msg_text(m), status_code=status_code)


@router.get("/password")
async def password_page(request: Request, m: str = ""):
    user = await _user_with_org(await require_user(request))
    return _password_page(user, m)


@router.post("/password")
async def password_submit(
    request: Request,
    old_password: str = Form(""),
    new_password: str = Form(""),
    confirm: str = Form(""),
):
    user = await _user_with_org(await require_user(request))
    key = f"pw|{user['id']}"
    if _throttled(key):
        logger.warning(f"🚫 改密限流: {user['username']}")
        return _password_page(user, "", status_code=429)

    if len(new_password) < 8:
        return _password_page(user, "pwshort", status_code=400)
    if new_password != confirm:
        return _password_page(user, "pwmismatch", status_code=400)
    if new_password == old_password:
        return _password_page(user, "pwsame", status_code=400)

    auth = await ext_user_service.get_auth(user["id"])
    if not auth or not verify_password(old_password, auth["pwd_salt"], auth["pwd_hash"]):
        _record_fail(key)
        logger.warning(f"❌ 改密失败（当前密码不对）: {user['username']}")
        return _password_page(user, "pwwrong", status_code=401)

    await ext_user_service.set_password(user["username"], new_password)
    _clear_fail(key)
    fresh = await ext_user_service.get(user["id"])  # 指纹已变
    resp = RedirectResponse(url="/ext/password?m=pwchanged", status_code=303)
    _issue_session(resp, fresh, _cookie_secure(request))  # 本机换成新会话，不被踢下线
    logger.info(f"🔑 外网账号 {user['username']} 修改了密码")
    return resp


# ── 首页 ─────────────────────────────────────────────


@router.get("")
@router.get("/")
async def home(request: Request):
    user = await _user_with_org(await require_user(request))
    scope = await load_scope(user["org_id"], user["id"])
    tiles = [
        ("/ext/devices", "📱", len(scope["device"]), "设备", "自动回复开关"),
        ("/ext/tables", "🗄️", len(scope["table"]), "表格", "只读查询"),
        ("/ext/agents", "🤖", len(scope["agent"]), "智能体", "模型与接口"),
        ("/ext/knowledge", "📚", len(scope["knowledge"]), "知识库", "详情与文档"),
    ]
    stats = "".join(
        f"""<a class="x-stat" href="{url}">
          <div class="x-stat-ic">{icon}</div>
          <div><div class="x-stat-n">{n}</div><div class="x-stat-l">{label} · {desc}</div></div>
        </a>"""
        for url, icon, n, label, desc in tiles
    )
    # LLM 推送快捷关闭：把"停掉机器人"这件事放在落地页，不用先导航到设备页
    push_html = ""
    if scope["device"]:
        rows = []
        on_count = 0
        for robot_id in sorted(scope["device"]):
            r = await robot_service.get_robot(robot_id)
            if not r:
                continue
            on_count += 1 if r.auto_reply else 0
            rows.append(
                f"""<tr>
                  <td data-label="设备"><div class="x-val">{esc(r.device_name or r.robot_id)}
                    <div class="x-sub">{esc(r.robot_id)}</div></div></td>
                  <td data-label="状态">{_status_pill(r.status)}</td>
                  <td data-label="LLM 推送">{_badge(r.auto_reply, "推送中" if r.auto_reply else "已关闭")}</td>
                  <td data-label="">{_push_switch(r.robot_id, r.auto_reply, "home")}</td>
                </tr>"""
            )
        total = len(rows)
        btn_disabled = "" if on_count else " disabled"
        push_html = f"""
    <div class="x-card">
      <div class="x-card-hd">
        <h2 class="x-card-t">LLM 推送快捷关闭</h2>
        <form method="POST" action="/ext/auto_reply_all">
          <input type="hidden" name="enabled" value="0">
          <input type="hidden" name="next" value="home">
          <button class="x-btn x-btn-danger x-btn-sm"{btn_disabled}
                  onclick="return confirm('关闭全部 {on_count} 台正在推送的设备？')">全部关闭</button>
        </form>
      </div>
      <p class="x-card-s">
        {'⚠️ 当前有 ' + str(on_count) + ' 台设备正在自动回复。' if on_count else '当前没有设备在自动回复。'}
        共 {total} 台可见设备；关闭后该设备立即停止自动回复，随时可以再打开。
      </p>
      <div class="x-scroll">
        <table class="x-table stack">
          <thead><tr><th>设备</th><th>状态</th><th>LLM 推送</th><th></th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </div>"""
    body = f"""
    {push_html}
    <div class="x-stats">{stats}</div>
    <div class="x-card">
      <h2 class="x-card-t">使用说明</h2>
      <div class="x-guide">
        <div><b>📱 设备</b><span class="x-hint">开关每台设备的自动回复；绑定哪个智能体由管理员维护</span></div>
        <div><b>🗄️ 表格</b><span class="x-hint">只读查询，数据由系统实时写入，始终是最新的</span></div>
        <div><b>🤖 智能体</b><span class="x-hint">调整模型与接口地址；提示词、采样参数由管理员维护</span></div>
        <div><b>📚 知识库</b><span class="x-hint">查看知识库详情与文档清单，内容增删改请联系管理员</span></div>
      </div>
    </div>"""
    return _page(user, "客户控制台", "欢迎回来", body, active="home",
                 subtitle=f"{esc(user['username'])} · {esc(user['org_name'])}")


# ── 设备：推送开关 + 智能体绑定 ──────────────────────


@router.get("/devices")
async def devices(request: Request, m: str = ""):
    user = await _user_with_org(await require_user(request))
    scope = await load_scope(user["org_id"], user["id"])

    cards = []
    for robot_id in sorted(scope["device"]):
        r = await robot_service.get_robot(robot_id)
        if not r:
            continue
        switch = _push_switch(r.robot_id, r.auto_reply, "devices")
        # 只读：客户只能看到当前绑定的智能体，不能改（绑定关系由管理员在内网维护）
        bind = f'<span>{esc(r.agent_name or "未绑定")}</span>' 
        cards.append(
            f"""
        <div class="x-dev">
          <div class="x-dev-hd">
            <div>
              <div class="x-dev-n">{esc(r.device_name or r.robot_id)}</div>
              <div class="x-dev-id">{esc(r.robot_id)}</div>
            </div>
            {_status_pill(r.status)}
          </div>
          <div class="x-dev-meta">
            <div class="x-dev-line"><span class="x-label">LLM 推送</span>{switch}</div>
            <div class="x-dev-line"><span class="x-label">绑定智能体</span>{bind}</div>
          </div>
        </div>"""
        )

    body = f"""
    <div class="x-card">
      <div class="x-card-hd"><h2 class="x-card-t">设备（{len(scope["device"])}）</h2></div>
      <p class="x-card-s">LLM 推送关闭后，该设备立即停止自动回复。绑定智能体决定它用哪套模型与提示词。</p>
      {f'<div class="x-devs">{"".join(cards)}</div>' if cards else '<div class="x-empty">本组织暂无授权设备</div>'}
    </div>"""
    return _page(user, "设备", "设备", body, msg=_msg_text(m), active="devices",
                 subtitle="控制每台设备的自动回复与所用智能体")


@router.post("/device/{robot_id}/auto_reply")
async def device_auto_reply(
    request: Request, robot_id: str, enabled: str = Form("1"), next: str = Form("devices")
):
    user = await require_user(request)
    scope = await load_scope(user["org_id"], user["id"])
    if not in_scope(scope, "device", robot_id):
        return _page(user, "无权访问", "无权访问", _DENY("该设备不在你的授权范围内"),
                     status_code=403)
    ok = await robot_service.update_auto_reply(robot_id, enabled == "1")
    logger.info(f"🌐 外网 {user['username']} 设置 {robot_id} 推送={enabled}")
    return RedirectResponse(
        url=f"{_next_url(next)}?m={'updated' if ok else 'fail'}", status_code=303
    )


@router.post("/auto_reply_all")
async def auto_reply_all(
    request: Request, enabled: str = Form("0"), next: str = Form("home")
):
    """批量开关本账号可见设备的 LLM 推送。只作用于授权范围内的设备。"""
    user = await require_user(request)
    scope = await load_scope(user["org_id"], user["id"])
    on = enabled == "1"
    done = 0
    # ponytail: 单账号可见设备一般个位数，顺序更新即可；上百台时再改并发/单条 UPDATE IN
    for robot_id in sorted(scope["device"]):
        if await robot_service.update_auto_reply(robot_id, on):
            done += 1
    logger.info(
        f"🌐 外网 {user['username']} 批量{'开启' if on else '关闭'}推送: "
        f"{done}/{len(scope['device'])} 台"
    )
    return RedirectResponse(
        url=f"{_next_url(next)}?m={'updated' if done else 'fail'}", status_code=303
    )


# ── 表格：只读查询 ───────────────────────────────────


@router.get("/tables")
async def tables(request: Request):
    user = await _user_with_org(await require_user(request))
    scope = await load_scope(user["org_id"], user["id"])
    all_tables = await table_service.list_tables()
    mine = [t for t in all_tables if in_scope(scope, "table", t.id)]
    rows = "".join(
        f"""<tr>
          <td data-label="表格"><div class="x-val"><b>{esc(t.name)}</b><div class="x-sub">{esc(t.table_key)}</div></div></td>
          <td data-label="说明">{esc(t.description) or '<span class="x-hint">—</span>'}</td>
          <td data-label="行数">{t.row_count}</td>
          <td data-label="">
            <a class="x-btn x-btn-outline x-btn-sm" href="/ext/table/{esc(t.table_key)}">查看数据</a>
            <a class="x-btn x-btn-ghost x-btn-sm" href="/ext/table/{esc(t.table_key)}/export">下载 CSV</a></td>
        </tr>"""
        for t in mine
    )
    empty = '<tr class="empty-row"><td colspan="4"><div class="x-empty">本组织暂无授权表格</div></td></tr>'
    body = f"""
    <div class="x-card">
      <p class="x-card-s">只读查询，数据由系统实时写入。共 {len(mine)} 张表。</p>
      <div class="x-scroll">
        <table class="x-table stack">
          <thead><tr><th>表格</th><th>说明</th><th>行数</th><th></th></tr></thead>
          <tbody>{rows or empty}</tbody>
        </table>
      </div>
    </div>"""
    return _page(user, "表格", "表格", body, active="tables",
                 subtitle=f"共 {len(mine)} 张表可查")


async def _table_or_deny(request: Request, table_key: str):
    """取当前账号可访问的表格；不可访问时返回 (user, None, 响应)。"""
    user = await _user_with_org(await require_user(request))
    scope = await load_scope(user["org_id"], user["id"])
    tbl = await table_service.get_table_by_key(table_key)
    if not tbl:
        return user, None, _page(user, "表格不存在", "表格不存在", _DENY("表格不存在或已被删除"),
                                 status_code=404)
    if not in_scope(scope, "table", tbl["id"]):
        return user, None, _page(user, "无权访问", "无权访问", _DENY("该表格不在你的授权范围内"),
                                 status_code=403)
    return user, tbl, None


def _table_cols(tbl: dict):
    cols = [f["name"] for f in tbl.get("fields", [])]
    labels = {f["name"]: (f.get("label") or f["name"]) for f in tbl.get("fields", [])}
    return cols, labels


# 字段类型 → 可用运算符（中文标签）；like 的 % 由服务端补，客户不必懂通配符
_ALL_OPS = [
    ("=", "等于"), ("!=", "不等于"), ("like", "包含"),
    (">", "大于"), ("<", "小于"), (">=", "不小于"), ("<=", "不大于"),
]
_NUM_OPS = [
    ("=", "等于"), ("!=", "不等于"),
    (">", "大于"), ("<", "小于"), (">=", "不小于"), ("<=", "不大于"),
]
_TYPE_OPS = {
    "int": _NUM_OPS,
    "float": _NUM_OPS,
    "datetime": [
        ("=", "等于（当天）"), (">", "晚于"), ("<", "早于"),
        (">=", "不早于"), ("<=", "不晚于"),
    ],
    "varchar": [("=", "等于"), ("!=", "不等于"), ("like", "包含")],
    "text": [("like", "包含"), ("=", "等于"), ("!=", "不等于")],
    "json": [("like", "包含"), ("=", "等于"), ("!=", "不等于")],
}


def _type_ops(ftype) -> list:
    """该类型可用的运算符；没选字段/未知类型给全集，否则客户会以为只有「等于」。"""
    return _TYPE_OPS.get(str(ftype or ""), _ALL_OPS)


def _field_type(tbl: dict, name: str):
    """字段类型（无此字段返回 None）。筛选运算符与输入控件都按它走。"""
    for f in tbl.get("fields", []):
        if f["name"] == name:
            return f.get("type")
    return None


# ponytail: 条件条数上限；真要十几条条件再改成分页/折叠
MAX_FILTER_ROWS = 5


def _filter_rows(params) -> list:
    """表单里的筛选条件行（至少一行）。

    行数由隐藏字段 n 携带：+添加条件 / ✕ 删除都只是改 n 再重新渲染，
    不需要前端 JS，也不需要把状态藏在会话里。
    """
    try:
        n = int(params.get("n") or 1)
    except ValueError:
        n = 1
    if params.get("add"):
        n += 1
    n = max(1, min(n, MAX_FILTER_ROWS))
    rows = [
        {
            "field": params.get(f"f{i}_field", ""),
            "op": params.get(f"f{i}_op", ""),
            "value": params.get(f"f{i}_value", ""),
        }
        for i in range(n)
    ]
    d = params.get("del")
    if d is not None and d.isdigit() and int(d) < len(rows):
        rows.pop(int(d))
    if not any(r["field"] for r in rows) and params.get("field"):
        # 兼容旧链接/书签：field/op/value 当作第一条
        rows[0] = {
            "field": params.get("field", ""),
            "op": params.get("op", ""),
            "value": params.get("value", ""),
        }
    return rows or [{"field": "", "op": "", "value": ""}]


def _one_cond(field: str, ftype, op: str, value: str):
    """单条条件 → (conds, 错误)。数字做类型校验，时间按整天展开成区间。"""
    if ftype in ("int", "float"):
        try:
            num = float(value)
        except ValueError:
            return None, "该字段是数字，请填数字"
        if ftype == "int" and not num.is_integer():
            return None, "该字段是整数，请填整数"
        return [{"field": field, "op": op, "value": int(num) if ftype == "int" else num}], ""
    if op == "like":
        return [{"field": field, "op": op, "value": str(value) if "%" in str(value) else f"%{value}%"}], ""
    if ftype == "datetime":
        # 时间列按「整天」比：等于某天=当天区间，晚于某天=次日 0 点起。
        # 否则「晚于 3-5」会把 3-5 上午的行也算进来，和「等于 3-5」看着自相矛盾。
        try:
            d = date.fromisoformat(str(value)[:10])
        except ValueError:
            return [{"field": field, "op": op, "value": value}], ""  # 认不出就按原样精确比
        day, nxt = d.isoformat(), (d + timedelta(days=1)).isoformat()
        return [
            {"field": field, "op": o, "value": v}
            for o, v in {
                "=": [(">=", day), ("<", nxt)],
                ">": [(">=", nxt)],
                ">=": [(">=", day)],
                "<": [("<", day)],
                "<=": [("<", nxt)],
            }[op]
        ], ""
    return [{"field": field, "op": op, "value": value}], ""


def _where_from_rows(tbl: dict, rows: list, strict: bool = False):
    """条件行 → (where, 错误列表, 归一化后的行)。

    多条条件之间是「并且」，同一字段写两条即区间（如 大于 5 且 小于 10）。
    strict=True（下载）时运算符与字段类型不匹配就报错；页面则静默回到「等于」，
    因为页面会把真正生效的运算符重新渲染出来，客户看得见。
    """
    conds, errs = [], []
    for row in rows:
        field, op = row["field"], (row["op"] or "=")
        if not field:
            continue
        ftype = _field_type(tbl, field)
        if ftype is None:
            errs.append(f"字段不存在：{field}")
            continue
        if not str(row["value"]).strip():
            continue  # 选了字段没填值 = 这条不生效
        if op not in {o for o, _ in _type_ops(ftype)}:
            if strict:
                errs.append(f"「{op}」不适用于「{field}」")
                continue
            op, row["op"] = "=", "="
        item, err = _one_cond(field, ftype, op, row["value"])
        if err:
            errs.append(err)
        elif item:
            conds.extend(item)
    return conds, errs, rows


def _table_where(tbl: dict, field: str, op: str, value: str):
    """单条件入口（下载/单测用）→ (where, 错误信息)。"""
    conds, errs, _ = _where_from_rows(
        tbl, [{"field": field, "op": op, "value": value}], strict=True
    )
    return (conds or None), "；".join(errs)


def _filter_row_html(i: int, row: dict, cols: list, labels: dict, tbl: dict, total: int) -> str:
    """一条筛选条件 = 字段 / 比较方式 / 值（+ 删除）。输入控件与运算符都按字段类型走。"""
    ftype = _field_type(tbl, row["field"])
    ops = _type_ops(ftype)
    op = row["op"] if row["op"] in {o for o, _ in ops} else ops[0][0]
    if ftype == "datetime":
        attrs, ph = ' type="date"', ""
    elif ftype in ("int", "float"):
        attrs = ' type="number"' + (' step="any"' if ftype == "float" else "")
        ph = "数字"
    else:
        attrs, ph = "", "要匹配的内容"
    del_btn = (
        f'<button class="x-btn x-btn-ghost x-btn-sm" name="del" value="{i}" title="删除这条条件">✕</button>'
        if total > 1 else ""
    )
    return f"""
        <div class="x-row" style="margin-bottom:8px">
          <div class="x-field" style="max-width:200px"><label class="x-label">字段</label>
            <select name="f{i}_field" class="x-select">
              <option value="">不过滤</option>
              {''.join(f'<option value="{esc(c)}"{" selected" if c == row["field"] else ""}>{esc(labels[c])}</option>' for c in cols)}
            </select></div>
          <div class="x-field" style="max-width:150px"><label class="x-label">比较方式</label>
            <select name="f{i}_op" class="x-select">
              {''.join(f'<option value="{o}"{" selected" if o == op else ""}>{esc(t)}</option>' for o, t in ops)}
            </select></div>
          <div class="x-field" style="max-width:220px"><label class="x-label">值</label>
            <input name="f{i}_value" class="x-input" value="{esc(row['value'])}" placeholder="{ph}"{attrs}></div>
          {del_btn}
        </div>"""


# ponytail: 下载一次性读进内存（上限 2 万行）；真有大表再改流式游标
_EXPORT_MAX = 20000


def _cell(v) -> str:
    if v is None:
        return ""
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    # CSV 公式注入防护：Excel 会把 =/+/-/@ 开头的单元格当公式执行
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def _csv_text(cols: list, labels: dict, rows: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([labels.get(c, c) for c in cols])
    for r in rows:
        w.writerow([_cell(r.get(c, "")) for c in cols])
    return "\ufeff" + buf.getvalue()  # BOM：Excel 才会按 UTF-8 打开中文


def _csv_headers(tbl: dict) -> dict:
    name = f"{tbl.get('name') or tbl.get('table_key') or 'table'}.csv"
    name = "".join(ch for ch in name if ch not in '\\/:*?"<>|\r\n') or "table.csv"
    # ASCII 回退名给老浏览器，filename* 给现代浏览器（中文表名）
    return {"Content-Disposition": f"attachment; filename=table.csv; filename*=UTF-8''{quote(name)}"}


@router.get("/table/{table_key}/export")
async def table_export(request: Request, table_key: str):
    """下载 CSV，沿用页面当前的筛选条件（不带参数=全量）。"""
    user, tbl, deny = await _table_or_deny(request, table_key)
    if deny:
        return deny
    where, errs, _ = _where_from_rows(
        tbl, _filter_rows(request.query_params), strict=True
    )
    if errs:
        return _page(user, "导出失败", tbl["name"], _DENY("；".join(errs)), status_code=400)
    rows = await table_service.query_rows(
        table_key, where=where, limit=_EXPORT_MAX, order_by="id", desc=True
    )
    cols, labels = _table_cols(tbl)
    logger.info(f"🌐 外网 {user['username']} 下载表格 {table_key}: {len(rows)} 行")
    return Response(
        content=_csv_text(cols, labels, rows),
        media_type="text/csv; charset=utf-8",
        headers=_csv_headers(tbl),
    )


@router.get("/table/{table_key}")
async def table_rows(request: Request, table_key: str, limit: int = 50):
    user, tbl, deny = await _table_or_deny(request, table_key)
    if deny:
        return deny

    limit = max(1, min(int(limit), 500))
    conds, errs, frows = _where_from_rows(tbl, _filter_rows(request.query_params))
    err = "；".join(errs)
    rows = [] if err else await table_service.query_rows(
        # ponytail: 固定按 id 倒序、不做翻页；客户要按别的列排序或要翻页时再加
        table_key, where=conds or None, limit=limit, order_by="id", desc=True
    )

    cols, labels = _table_cols(tbl)
    head = "".join(f"<th>{esc(labels[c])}</th>" for c in cols)
    filters_html = "".join(
        _filter_row_html(i, r, cols, labels, tbl, len(frows)) for i, r in enumerate(frows)
    )
    more_btn = (
        f'<button class="x-btn x-btn-ghost x-btn-sm" name="add" value="1">＋ 添加条件</button>'
        if len(frows) < MAX_FILTER_ROWS else ""
    )
    trs = []
    for r in rows:
        tds = "".join(
            f'<td data-label="{esc(labels[c])}">{esc(r.get(c, ""))}</td>' for c in cols
        )
        trs.append(f"<tr>{tds}</tr>")
    empty_row = f'<tr class="empty-row"><td colspan="{max(len(cols), 1)}"><div class="x-empty">没有符合条件的数据</div></td></tr>'
    body = f"""
    <div class="x-card">
      <div class="x-card-hd">
        <h2 class="x-card-t">数据（最多 {limit} 行）</h2>
        <a class="x-btn x-btn-ghost x-btn-sm" href="/ext/tables">← 返回表格列表</a>
      </div>
      <p class="x-card-s">{esc(tbl.get("description") or "只读查询")}　数据由系统实时写入。</p>
      <form method="GET" action="/ext/table/{esc(table_key)}" style="margin-bottom:10px">
        <input type="hidden" name="n" value="{len(frows)}">
        {filters_html}
        <div class="x-row">
          <div class="x-field" style="max-width:120px"><label class="x-label">返回行数</label>
            <input name="limit" class="x-input" value="{limit}"></div>
          <button class="x-btn x-btn-primary">查询</button>
          <button class="x-btn x-btn-outline" formaction="/ext/table/{esc(table_key)}/export"
                  title="按当前筛选条件导出 CSV（最多 {_EXPORT_MAX} 行）">⬇ 下载 CSV</button>
          {more_btn}
        </div>
      </form>
      <p class="x-hint" style="margin:0 0 16px">多个条件之间是「并且」（同一字段写两条即区间，如 大于 5 且 小于 10）。
        时间按整天筛选：点日历选日期（如 2026-08-09），填 202608091205 这种数字串认不出来，
        「等于某天」= 当天 0 点到次日 0 点前。</p>
      {f'<div class="x-alert x-alert-err">{esc(err)}</div>' if err else ''}
      <div class="x-scroll">
        <table class="x-table stack"><thead><tr>{head}</tr></thead>
        <tbody>{''.join(trs) or empty_row}</tbody></table>
      </div>
    </div>"""
    return _page(user, tbl["name"], tbl["name"], body, active="tables",
                 subtitle=f"共返回 {len(rows)} 行")


# ── 智能体：模型配置（不含 Key）──────────────────────


@router.get("/agents")
async def agents(request: Request):
    user = await _user_with_org(await require_user(request))
    scope = await load_scope(user["org_id"], user["id"])
    mine = [a for a in await agent_service.list_agents() if in_scope(scope, "agent", a.id)]
    rows = "".join(
        f"""<tr>
          <td data-label="名称"><div class="x-val"><b>{esc(a.name)}</b><div class="x-sub">{esc(a.description or "")}</div></div></td>
          <td data-label="服务商">{esc(a.provider)}</td>
          <td data-label="模型">{esc(a.model)}</td>
          <td data-label="状态">{_badge(a.enabled)}</td>
          <td data-label="">
            <a class="x-btn x-btn-outline x-btn-sm" href="/ext/agent/{a.id}">配置</a></td>
        </tr>"""
        for a in mine
    )
    empty = '<tr class="empty-row"><td colspan="5"><div class="x-empty">本组织暂无授权智能体</div></td></tr>'
    body = f"""
    <div class="x-card">
      <p class="x-card-s">共 {len(mine)} 个智能体。API Key 由管理员在内网维护，此处不可见、也不可修改。</p>
      <div class="x-scroll">
        <table class="x-table stack">
          <thead><tr><th>名称</th><th>服务商</th><th>模型</th><th>状态</th><th></th></tr></thead>
          <tbody>{rows or empty}</tbody>
        </table>
      </div>
    </div>"""
    return _page(user, "智能体", "智能体", body, active="agents",
                 subtitle=f"共 {len(mine)} 个智能体")


@router.get("/agent/{agent_id}")
async def agent_edit(request: Request, agent_id: int, m: str = ""):
    user = await _user_with_org(await require_user(request))
    scope = await load_scope(user["org_id"], user["id"])
    a = await agent_service.get_agent(agent_id)
    if not a:
        return _page(user, "不存在", "不存在", _DENY("智能体不存在或已被删除"), status_code=404)
    if not in_scope(scope, "agent", agent_id):
        return _page(user, "无权访问", "无权访问", _DENY("该智能体不在你的授权范围内"),
                     status_code=403)

    body = f"""
    <div class="x-card">
      <div class="x-card-hd">
        <h2 class="x-card-t">模型配置</h2>
        <a class="x-btn x-btn-ghost x-btn-sm" href="/ext/agents">← 返回列表</a>
      </div>
      <p class="x-card-s">只能调整服务商、模型与接口地址；其余配置与 API Key 由管理员维护。</p>
      <form method="POST" action="/ext/agent/{a.id}">
        <div class="x-row">
          <div class="x-field"><label class="x-label">服务商</label>
            <input class="x-input" name="provider" value="{esc(a.provider)}" placeholder="deepseek / openai / qwen …"></div>
          <div class="x-field"><label class="x-label">模型</label>
            <input class="x-input" name="model" value="{esc(a.model)}" placeholder="deepseek-v4-flash"></div>
        </div>
        <div class="x-row" style="margin-top:14px">
          <div class="x-field"><label class="x-label">接口地址 base_url</label>
            <input class="x-input" name="base_url" value="{esc(a.base_url)}" placeholder="https://api.deepseek.com/v1"></div>
        </div>
        <div style="margin-top:18px"><button class="x-btn x-btn-primary">保存配置</button></div>
      </form>
    </div>

    <div class="x-card">
      <h2 class="x-card-t">只读配置</h2>
      <p class="x-card-s">以下配置由管理员在内网维护，客户控制台不可修改。</p>
      <div class="x-scroll">
        <table class="x-table kv">
          <tbody>
            <tr><td class="x-key">状态</td><td>{_badge(a.enabled)}</td></tr>
            <tr><td class="x-key">API Key</td><td>{('••••' + a.api_key[-4:]) if a.api_key else '未设置'}</td></tr>
            <tr><td class="x-key">temperature</td><td>{a.temperature}</td></tr>
            <tr><td class="x-key">max_tokens</td><td>{a.max_tokens}</td></tr>
            <tr><td class="x-key">上下文轮数</td><td>{a.max_context_turns}</td></tr>
            <tr><td class="x-key">描述</td><td>{esc(a.description) or '<span class="x-hint">—</span>'}</td></tr>
          </tbody>
        </table>
      </div>
    </div>"""
    return _page(user, a.name, a.name, body, msg=_msg_text(m), active="agents",
                 subtitle="只能调整模型与接口地址")


@router.post("/agent/{agent_id}")
async def agent_update(
    request: Request,
    agent_id: int,
    provider: str = Form(""),
    model: str = Form(""),
    base_url: str = Form(""),
):
    user = await require_user(request)
    scope = await load_scope(user["org_id"], user["id"])
    if not in_scope(scope, "agent", agent_id):
        return _page(user, "无权访问", "无权访问", _DENY("该智能体不在你的授权范围内"),
                     status_code=403)
    a = await agent_service.get_agent(agent_id)
    if not a:
        return _page(user, "不存在", "不存在", _DENY("智能体不存在或已被删除"), status_code=404)

    # 只允许改 服务商/模型/接口地址：其余字段一律取数据库现值，
    # 构造带 temperature/system_prompt 的请求也改不动（不是"界面上藏起来"而已）
    req = AgentCreateRequest(
        name=a.name,
        description=a.description,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key="",
        system_prompt=a.system_prompt,
        temperature=a.temperature,
        max_tokens=a.max_tokens,
        max_context_turns=a.max_context_turns,
        idle_reset_minutes=a.idle_reset_minutes,
        image_strategy=a.image_strategy,
        enable_tools=a.enable_tools,
        enable_http=a.enable_http,
        allow_table_create=a.allow_table_create,
        allow_task_create=a.allow_task_create,
        enabled=a.enabled,
    )
    try:
        ok = await agent_service.update_agent(agent_id, req)
    except Exception as e:
        logger.warning(f"外网更新智能体失败 {agent_id}: {e}")
        ok = False
    logger.info(f"🌐 外网 {user['username']} 更新智能体 {agent_id}: ok={ok}")
    return RedirectResponse(
        url=f"/ext/agent/{agent_id}?m={'ok' if ok else 'invalid'}", status_code=303
    )


# ── 知识库：列表 + 检索测试 ──────────────────────────


@router.get("/knowledge")
async def knowledge(request: Request):
    user = await _user_with_org(await require_user(request))
    scope = await load_scope(user["org_id"], user["id"])
    mine = [k for k in await knowledge_service.list_kbs() if in_scope(scope, "knowledge", k.id)]
    rows = "".join(
        f"""<tr>
          <td data-label="名称"><div class="x-val"><b>{esc(k.name)}</b><div class="x-sub">{esc(k.description or "")}</div></div></td>
          <td data-label="类型">{_KB_TYPE.get(k.kb_type, esc(k.kb_type))}</td>
          <td data-label="状态">{_badge(k.enabled)}</td>
          <td data-label="文档数">{k.doc_count}</td>
          <td data-label="片段数">{k.chunk_count}</td>
          <td data-label="">
            <a class="x-btn x-btn-outline x-btn-sm" href="/ext/knowledge/{k.id}">查看详情</a></td>
        </tr>"""
        for k in mine
    )
    empty = '<tr class="empty-row"><td colspan="6"><div class="x-empty">本组织暂无授权知识库</div></td></tr>'
    body = f"""
    <div class="x-card">
      <p class="x-card-s">共 {len(mine)} 个知识库。可查看详情与文档清单；内容的增删改由管理员在内网完成。</p>
      <div class="x-scroll">
        <table class="x-table stack">
          <thead><tr><th>名称</th><th>类型</th><th>状态</th><th>文档数</th><th>片段数</th><th></th></tr></thead>
          <tbody>{rows or empty}</tbody>
        </table>
      </div>
    </div>"""
    return _page(user, "知识库", "知识库", body, active="knowledge",
                 subtitle=f"共 {len(mine)} 个知识库")


@router.get("/knowledge/{kb_id}")
async def knowledge_detail(request: Request, kb_id: int, m: str = ""):
    """知识库详情：元信息 + 文档清单。只读，不提供检索入口（检索由智能体在回复时自动完成）。"""
    user = await _user_with_org(await require_user(request))
    scope = await load_scope(user["org_id"], user["id"])
    if not in_scope(scope, "knowledge", kb_id):
        return _page(user, "无权访问", "无权访问", _DENY("该知识库不在你的授权范围内"),
                     status_code=403)
    kb = await knowledge_service.get_kb(kb_id)
    if not kb:
        return _page(user, "不存在", "不存在", _DENY("知识库不存在或已被删除"), status_code=404)

    is_local = kb.kb_type == "local"
    docs = await knowledge_service.list_documents(kb_id) if is_local else []

    if is_local:
        rows = "".join(
            f"""<tr>
              <td data-label="文件名">{esc(d.get("filename") or "-")}</td>
              <td data-label="片段数">{d.get("chunk_count") or 0}</td>
              <td data-label="上传时间">{esc((d.get("created_at") or "")[:19])}</td>
              <td data-label="">
                <form method="POST" action="/ext/knowledge/{kb_id}/doc/{d.get("id")}/delete"
                      onsubmit="return confirm('删除文档「{esc(d.get("filename") or '')}」？删除后其片段一并移除。')">
                  <button class="x-btn x-btn-danger x-btn-sm">删除</button>
                </form></td>
            </tr>"""
            for d in docs
        ) or '<tr class="empty-row"><td colspan="4"><div class="x-empty">该知识库还没有文档</div></td></tr>'
        docs_block = f"""
    <div class="x-card">
      <div class="x-card-hd"><h2 class="x-card-t">文档管理（{len(docs)}）</h2></div>
      <p class="x-card-s">支持 txt / md / csv / json / log / pdf / docx，单个文件不超过 {_MAX_UPLOAD_MB}MB。
      上传后自动切分入库，智能体回复时会检索到新内容。</p>
      <form method="POST" action="/ext/knowledge/{kb_id}/upload" enctype="multipart/form-data"
            class="x-row" style="margin-bottom:16px">
        <div class="x-field"><label class="x-label">选择文件</label>
          <input class="x-input" type="file" name="file" required
                 accept=".txt,.md,.csv,.json,.log,.pdf,.docx"></div>
        <button class="x-btn x-btn-primary">上传并切分</button>
      </form>
      <div class="x-scroll">
        <table class="x-table stack">
          <thead><tr><th>文件名</th><th>片段数</th><th>上传时间</th><th></th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>"""
    else:
        docs_block = f"""
    <div class="x-card">
      <h2 class="x-card-t">文档管理</h2>
      <p class="x-card-s">☁️ 这是{_KB_TYPE.get(kb.kb_type, esc(kb.kb_type))}，文档在对应平台侧维护，
      本地不保存副本，因此这里不提供文档列表与上传入口。</p>
    </div>"""

    # config 里可能有 api_key：只挑白名单字段展示，绝不整块输出
    cfg = kb.config or {}
    cfg_items = []
    if kb.kb_type == "local":
        cfg_items = [("Embedding 模型", cfg.get("embedding_model") or "未配置（关键词检索）")]
    elif kb.kb_type == "bailian":
        cfg_items = [
            ("工作空间 ID", cfg.get("workspace_id") or "-"),
            ("知识库名称", cfg.get("collection_name") or "-"),
        ]
    elif kb.kb_type == "http":
        cfg_items = [("检索接口", cfg.get("url") or "-")]
    cfg_rows = "".join(
        f'<tr><td class="x-key">{esc(k)}</td><td>{esc(str(v))}</td></tr>'
        for k, v in cfg_items
    )
    meta_rows = f"""
        <tr><td class="x-key">类型</td><td>{_KB_TYPE.get(kb.kb_type, esc(kb.kb_type))}</td></tr>
        <tr><td class="x-key">状态</td><td>{_badge(kb.enabled)}</td></tr>
        <tr><td class="x-key">创建时间</td><td>{esc((kb.created_at or "")[:19])}</td></tr>
        <tr><td class="x-key">文档数 / 片段数</td><td>{kb.doc_count} / {kb.chunk_count}</td></tr>
        <tr><td class="x-key">检索返回条数 top_k</td><td>{kb.top_k}</td></tr>
        <tr><td class="x-key">切分长度 / 重叠</td><td>{kb.chunk_size} / {kb.chunk_overlap}</td></tr>
        {cfg_rows}"""

    body = f"""
    <div class="x-card">
      <div class="x-card-hd">
        <h2 class="x-card-t">{esc(kb.name)}</h2>
        <a class="x-btn x-btn-ghost x-btn-sm" href="/ext/knowledge">← 返回知识库</a>
      </div>
      <p class="x-card-s">{esc(kb.description) or "暂无描述"}</p>
      <div class="x-scroll"><table class="x-table kv"><tbody>{meta_rows}</tbody></table></div>
    </div>

    {docs_block}"""
    return _page(user, kb.name, kb.name, body, msg=_msg_text(m), active="knowledge",
                 subtitle="知识库详情（只读）")


async def _kb_or_deny(request: Request, kb_id: int):
    """取当前账号可访问的知识库；不可访问时返回 (None, 响应)。"""
    user = await _user_with_org(await require_user(request))
    scope = await load_scope(user["org_id"], user["id"])
    if not in_scope(scope, "knowledge", kb_id):
        return user, None, _page(user, "无权访问", "无权访问", _DENY("该知识库不在你的授权范围内"),
                                 status_code=403)
    kb = await knowledge_service.get_kb(kb_id)
    if not kb:
        return user, None, _page(user, "不存在", "不存在", _DENY("知识库不存在或已被删除"),
                                 status_code=404)
    return user, kb, None


@router.post("/knowledge/{kb_id}/upload")
async def knowledge_upload(request: Request, kb_id: int, file: UploadFile = File(...)):
    """给本地知识库上传文档。云知识库（bailian/http）不接受上传。"""
    user, kb, deny = await _kb_or_deny(request, kb_id)
    if deny:
        return deny
    if kb.kb_type != "local":
        return RedirectResponse(
            url=f"/ext/knowledge/{kb_id}?m=cloud", status_code=303
        )

    filename = os.path.basename(file.filename or "")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_DOC_EXT:
        logger.warning(f"外网 {user['username']} 上传被拒（类型）: {filename!r}")
        return RedirectResponse(url=f"/ext/knowledge/{kb_id}?m=badext", status_code=303)

    # 分块读取并在超限时立刻中止，避免"先读进内存再判断大小"
    cap = _MAX_UPLOAD_MB * 1024 * 1024
    buf = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > cap:
            logger.warning(f"外网 {user['username']} 上传被拒（超限）: {filename}")
            return RedirectResponse(url=f"/ext/knowledge/{kb_id}?m=toobig", status_code=303)

    try:
        chunks = await knowledge_service.upload_document(kb_id, filename, bytes(buf))
    except ValueError as e:
        logger.warning(f"外网上传解析失败 {filename}: {e}")
        return RedirectResponse(url=f"/ext/knowledge/{kb_id}?m=empty", status_code=303)
    except Exception as e:
        logger.error(f"外网上传失败 {filename}: {e}")
        return RedirectResponse(url=f"/ext/knowledge/{kb_id}?m=failed", status_code=303)

    logger.info(
        f"🌐 外网 {user['username']} 上传文档到知识库 {kb_id}: {filename} → {chunks} 片段"
    )
    return RedirectResponse(url=f"/ext/knowledge/{kb_id}?m=uploaded", status_code=303)


@router.post("/knowledge/{kb_id}/doc/{doc_id}/delete")
async def knowledge_doc_delete(request: Request, kb_id: int, doc_id: int):
    user, kb, deny = await _kb_or_deny(request, kb_id)
    if deny:
        return deny
    if kb.kb_type != "local":
        return RedirectResponse(url=f"/ext/knowledge/{kb_id}?m=cloud", status_code=303)

    # 必须确认这篇文档属于本知识库（delete_document 只按 doc_id 删，不做归属校验）
    own = {int(d.get("id") or 0) for d in await knowledge_service.list_documents(kb_id)}
    if int(doc_id) not in own:
        logger.warning(f"外网 {user['username']} 试图删除不属于知识库 {kb_id} 的文档 {doc_id}")
        return RedirectResponse(url=f"/ext/knowledge/{kb_id}?m=nodoc", status_code=303)

    ok = await knowledge_service.delete_document(doc_id)
    logger.info(f"🌐 外网 {user['username']} 删除知识库 {kb_id} 文档 {doc_id}: {ok}")
    return RedirectResponse(
        url=f"/ext/knowledge/{kb_id}?m={'deleted' if ok else 'nodoc'}", status_code=303
    )

# ── 长期记忆（/ext 里唯一"客户可写"的资源） ────────────────────────────
#
# 权限模型：记忆集本身**只读**（不能新建/删除/改描述），客户名单也只读（由智能体写入时
# 自动建立，或管理员在内网维护）；客户在这里只能增删改**记忆**本身，包含不针对具体客户的
# 「通用记忆」（customer_id=0，对该套记忆下的所有客户会话都注入）。
# 每条路由都做两级校验：可见范围（组织 ∩ 账号范围）→ 归属（条目/客户必须属于该记忆集）。


async def _mem_set_or_deny(request: Request, set_id: int):
    """取当前账号可访问的记忆集；不可访问时返回 (user, None, 响应)。"""
    user = await _user_with_org(await require_user(request))
    scope = await load_scope(user["org_id"], user["id"])
    if not in_scope(scope, "memory", set_id):
        logger.warning(f"外网 {user['username']} 访问未授权记忆集 {set_id}")
        return user, None, _page(
            user, "无权访问", "无权访问", _DENY("该记忆集不在你的授权范围内"), status_code=403
        )
    st = await memory_service.get_set(set_id)
    if not st:
        return user, None, _page(
            user, "不存在", "不存在", _DENY("记忆集不存在或已被删除"), status_code=404
        )
    return user, st, None


def _mem_item_row(it: dict, set_id: int, edit_item: int) -> str:
    """一条记忆的展示行；edit_item == 该条 id 时就地变成编辑表单。"""
    if int(it["id"]) == int(edit_item or 0):
        return f"""<tr><td colspan="3">
          <form method="POST" action="/ext/memory/{set_id}/item/{it['id']}/update">
            <div class="x-field"><label class="x-label">修改记忆</label>
              <textarea class="x-textarea" name="content" maxlength="500" required>{esc(it['content'])}</textarea></div>
            <div style="display:flex;gap:8px;margin-top:10px">
              <button class="x-btn x-btn-primary x-btn-sm">保存</button>
              <a class="x-btn x-btn-ghost x-btn-sm" href="/ext/memory/{set_id}">取消</a>
            </div>
          </form></td></tr>"""
    src = "智能体写入" if it["source"] == "agent" else "人工写入"
    when = esc((it["updated_at"] or it["created_at"] or "")[:16].replace("T", " "))
    return f"""<tr>
      <td data-label="记忆"><div class="x-val">{esc(it['content'])}</div></td>
      <td data-label="来源"><span class="x-pill x-pill-info">{src}</span><div class="x-sub">{when}</div></td>
      <td data-label="">
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <a class="x-btn x-btn-ghost x-btn-sm" href="/ext/memory/{set_id}?edit_item={it['id']}">编辑</a>
          <form method="POST" action="/ext/memory/{set_id}/item/{it['id']}/delete"
                onsubmit="return confirm('删除这条记忆？')">
            <button class="x-btn x-btn-danger x-btn-sm">删除</button>
          </form>
        </div></td></tr>"""


def _mem_items_table(items: list, set_id: int, edit_item: int, empty_text: str) -> str:
    rows = "".join(_mem_item_row(it, set_id, edit_item) for it in items)
    empty = f'<tr class="empty-row"><td colspan="3"><div class="x-empty">{esc(empty_text)}</div></td></tr>'
    return f"""<div class="x-scroll"><table class="x-table stack">
      <thead><tr><th>记忆</th><th>来源</th><th></th></tr></thead>
      <tbody>{rows or empty}</tbody>
    </table></div>"""


async def _cleanup_note(set_id: int) -> str:
    """记忆清理的只读说明：频率 + 用哪个智能体（控制台不支持改，改要去内网管理端）。"""
    st = await memory_service.get_set(set_id) or {}
    hours = int(st.get("cleanup_hours") or 0)
    agent_id = int(st.get("cleanup_agent_id") or 0)
    if hours <= 0 or not agent_id:
        return ('<span class="x-pill">记忆清理：不清理</span> '
                '<span style="color:var(--text-2)">（智能体写入的事实会一直保留，不会自动整理；需要开启请联系管理员在内网设置）</span>')
    name = f"#{agent_id}"
    try:
        agent = await agent_service.get_agent(agent_id)
        if agent is not None:
            name = getattr(agent, "name", "") or name
    except Exception:
        logger.warning(f"读取清理智能体失败 #{agent_id}", exc_info=True)
    return (f'<span class="x-pill x-pill-info">记忆清理：{esc(cleanup_freq_label(hours))}</span> '
            f'<span style="color:var(--text-2)">由「<b>{esc(name)}</b>」定期把智能体写入的事实压缩合并；'
            '人工补录与批量导入的事实不参与（只作为参考）。此配置只读，需要调整请联系管理员。</span>')


@router.get("/memory")
async def memory_sets(request: Request):
    """记忆集列表：只显示落在可见范围内的（记忆集不可新建/删除，只能进去管理记忆）。"""
    user = await _user_with_org(await require_user(request))
    scope = await load_scope(user["org_id"], user["id"])
    mine = [s for s in await memory_service.list_sets() if in_scope(scope, "memory", s["id"])]
    notes = {int(s["id"]): await _cleanup_note(int(s["id"])) for s in mine}
    rows = "".join(
        f"""<tr>
          <td data-label="名称"><div class="x-val"><b>{esc(s['name'])}</b>
            <div class="x-sub">{esc(s['description'] or '')}</div></div></td>
          <td data-label="规模">{s['customer_count']} 位客户 / {s['item_count']} 条记忆</td>
          <td data-label="清理">{notes[int(s['id'])]}</td>
          <td data-label=""><a class="x-btn x-btn-outline x-btn-sm" href="/ext/memory/{s['id']}">管理记忆</a></td>
        </tr>"""
        for s in mine
    )
    empty = '<tr class="empty-row"><td colspan="4"><div class="x-empty">本组织暂无授权记忆集</div></td></tr>'
    body = f"""
    <div class="x-card">
      <p class="x-card-s">共 {len(mine)} 套记忆。记忆集与客户由管理员在内网维护（这里不能新建或删除记忆集）；
      每一套里的<b>记忆</b>可以在这里增删改，包括不针对具体客户、对所有会话都生效的「通用记忆」。</p>
      <div class="x-scroll">
        <table class="x-table stack">
          <thead><tr><th>名称</th><th>规模</th><th>记忆清理</th><th></th></tr></thead>
          <tbody>{rows or empty}</tbody>
        </table>
      </div>
    </div>"""
    return _page(user, "长期记忆", "长期记忆", body, active="memory",
                 subtitle=f"共 {len(mine)} 套记忆")


@router.get("/memory/{set_id}")
async def memory_detail(request: Request, set_id: int, m: str = "", edit_item: int = 0,
                       q: str = "", all: int = 0, a: int = -1, d: int = -1, sk: int = -1):
    """一套记忆的详情：通用记忆 + 按客户分组的记忆，可就地增删改。

    记忆一多页面会很长，所以：客户区块**默认折叠**（`<details>`），每个客户默认只渲染最近
    `ITEMS_PER_CUSTOMER` 条（要清旧账就点「显示全部」，`all=1`），并支持按客户名筛选（`q=`）。
    正在编辑的那条一定会被渲染出来（哪怕它不在最近 N 条里）。
    """
    user, st, deny = await _mem_set_or_deny(request, set_id)
    if deny:
        return deny
    customers = await memory_service.list_customers(set_id)
    items = await memory_service.list_items(set_id)
    general = [i for i in items if not i["customer_id"]]
    by_customer = {int(c["id"]): [] for c in customers}
    for it in items:
        if int(it["customer_id"]) in by_customer:
            by_customer[int(it["customer_id"])].append(it)

    total_customers = len(customers)          # 概览里的总数要用过滤前的
    q = (q or "").strip()
    if q:
        customers = [c for c in customers if q in (c["name"] or "") or q in (c["aliases"] or "")]

    def _slice(rows: list) -> tuple:
        """按显示上限切片；保证正在编辑的那条在里面。返回 (可见条目, 被隐藏的条数)。"""
        if all or len(rows) <= ITEMS_PER_CUSTOMER:
            return rows, 0
        shown = rows[:ITEMS_PER_CUSTOMER]
        if edit_item and not any(int(i["id"]) == int(edit_item) for i in shown):
            extra = next((i for i in rows if int(i["id"]) == int(edit_item)), None)
            if extra:
                shown = shown + [extra]
        return shown, len(rows) - len(shown)

    full = len(items) >= SET_MAX_ITEMS
    limits = "".join(f"<li>{esc(k)}：{esc(v)}</li>" for k, v in MEMORY_LIMITS)
    def _section(title: str, rows: list, subtitle_html: str, empty_text: str, open_it: bool,
                 extra_html: str = "") -> str:
        shown, hidden = _slice(rows)
        hint = ""
        if hidden:
            hint = (f'<p class="x-card-s" style="margin:6px 0 0">仅显示最近 {len(shown)} 条，'
                    f'还有 {hidden} 条较早的记忆 · '
                    f'<a href="/ext/memory/{set_id}?all=1{("&q=" + q) if q else ""}">显示全部</a></p>')
        return f"""<div class="x-card">
          <details{' open' if (open_it or not rows) else ''}>
            <summary style="cursor:pointer;user-select:none"><b>{title}</b>
              <span class="x-pill x-pill-info">{len(rows)} 条</span>{subtitle_html}</summary>
            <div style="margin-top:10px">{_mem_items_table(shown, set_id, edit_item, empty_text)}</div>
            {hint}
            {extra_html}
          </details></div>"""

    filter_form = f"""<div class="x-card"><form method="GET" action="/ext/memory/{set_id}"
        style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
        <div class="x-field" style="max-width:260px"><label class="x-label">按客户筛选</label>
          <input class="x-input" name="q" value="{esc(q)}" placeholder="客户名或别名，留空看全部"></div>
        {'<input type="hidden" name="all" value="1">' if all else ''}
        <button class="x-btn x-btn-outline">筛选</button>
        {f'<a class="x-btn x-btn-ghost" href="/ext/memory/{set_id}">清空</a>' if q else ''}
      </form></div>"""

    customer_card = f"""<div class="x-card"><h3 style="margin:0 0 10px">👤 新增客户</h3>
        <p class="x-card-s">只建客户、不写记忆（记忆可以以后再补，或直接用下面的批量导入）。
        <b>只看规范名</b>：名字已存在就复用该客户，名字撞上别人的别名则另建一位。</p>
        <form method="POST" action="/ext/memory/{set_id}/customer/create">
          <div style="display:flex;gap:12px;flex-wrap:wrap">
            <div class="x-field" style="max-width:240px"><label class="x-label">客户规范名 *</label>
              <input class="x-input" name="name" required maxlength="{CUSTOMER_NAME_MAX}" placeholder="如：张三"></div>
            <div class="x-field" style="max-width:300px"><label class="x-label">别名（可选，逗号分隔；填了以本次为准覆盖）</label>
              <input class="x-input" name="aliases" placeholder="如：张三-采购,张三(XX公司)"></div>
          </div>
          <div style="margin-top:12px"><button class="x-btn x-btn-primary"
            {'disabled' if full else ''}>添加客户</button></div>
        </form></div>"""

    import_card = f"""<div class="x-card"><details>
        <summary style="cursor:pointer;user-select:none"><b>📥 批量导入记忆</b>
          <span class="x-sub">一行一条 · 把以往的客户资料一次录进来</span></summary>
        <div style="margin-top:10px">
          <p class="x-card-s"><b>客户只看规范名</b>（不看别名：名字撞上别人的别名会另建客户）；
          <b>填了客户名就会建/找到这位客户</b>，<b>事实留空 = 只建客户</b>（等于上面的「新增客户」）；
          别名<b>填了就以导入为准覆盖</b>、留空则不动该客户已有别名；
          事实<b>只追加、不覆盖</b>（同客户同内容判重跳过）。</p>
          <form method="POST" action="/ext/memory/{set_id}/import">
            <div style="display:flex;gap:12px;flex-wrap:wrap">
              <div class="x-field" style="max-width:240px"><label class="x-label">客户名（留空 = 通用记忆）</label>
                <input class="x-input" name="customer_name" maxlength="{CUSTOMER_NAME_MAX}" placeholder="如：张三"></div>
              <div class="x-field" style="max-width:240px"><label class="x-label">别名（可选，逗号分隔）</label>
                <input class="x-input" name="aliases" placeholder="如：张三-采购,张三(XX公司)"></div>
            </div>
            <div class="x-field" style="margin-top:10px"><label class="x-label">事实（一行一条，单行超过 500 字会跳过）</label>
              <textarea class="x-textarea" name="text" rows="6"
                placeholder="2026-08 询问过 A 型号报价，预算 5 万以内&#10;只接受账期 60 天&#10;技术对接人是李工"></textarea></div>
            <div style="margin-top:12px"><button class="x-btn x-btn-primary"
              {'disabled' if full else ''}>导入</button></div>
          </form>
        </div>
      </details></div>"""

    general_add_form = f"""<form method="POST" action="/ext/memory/{set_id}/item/create" style="margin-top:10px">
        <input type="hidden" name="customer_id" value="0">
        <div class="x-field"><label class="x-label">添加通用记忆（对所有客户会话都注入）</label>
          <input class="x-input" name="content" required maxlength="500"
            placeholder="例：公司统一开票信息 / 报价一律含税"></div>
        <div style="margin-top:10px"><button class="x-btn x-btn-outline"
          {'disabled' if full else ''}>添加</button>
          {'<span class="x-sub">记忆集已满，需先清理</span>' if full else ''}</div>
      </form>"""

    # 区块顺序（用户指定）：概览 → **通用记忆（自带添加）** → 新增客户 → 批量导入 → **客户筛选** → 客户记忆
    blocks = [
        f"""<div class="x-card">
          <p class="x-card-s">本套「{esc(st['name'])}」现有 <b>{total_customers}</b>
          位客户、<b>{len(items)}</b> 条记忆（上限 {SET_MAX_ITEMS} 条{f'，<b>已满</b>' if full else ''}）。
          超限只会被拒绝，不会静默丢数据。</p>
          <p style="margin:10px 0 0">{await _cleanup_note(set_id)}</p>
          <details style="margin-top:10px">
            <summary style="cursor:pointer;user-select:none">🛡️ 护栏与阈值说明</summary>
            <ul style="margin:8px 0 0;padding-left:18px;color:var(--text-2);font-size:13px;line-height:1.9">{limits}</ul>
          </details>
        </div>""",
        _section("🌐 通用记忆", general,
                 '<div class="x-sub">不针对具体客户，所有客户会话都注入</div>',
                 "暂无通用记忆", True, extra_html=general_add_form),
        customer_card,
        import_card,
        filter_form,
    ]
    for c in customers:
        alias = f'<div class="x-sub">别名：{esc(c["aliases"])}</div>' if c["aliases"] else ""
        rows = by_customer[int(c["id"])]
        open_it = bool(edit_item) and any(int(i["id"]) == int(edit_item) for i in rows)
        blocks.append(_section(f"👤 {esc(c['name'])}", rows, alias, "这位客户暂无记忆", open_it))
    if not customers:
        blocks.append(
            '<div class="x-card"><div class="x-empty">'
            + ("没有匹配的客户（换个关键词试试）" if q else
               "这套记忆还没有客户 —— 客户由智能体在对话中自动建立，也可以在「新增记忆」里选"
               "「其他：自定义客户名」直接建")
            + "</div></div>"
        )

    body = "".join(blocks)
    msg = (f"✅ 导入完成：新增 {a} 条，重复 {d} 条，跳过 {sk} 条"
           if (m == "memimp" and a >= 0) else _msg_text(m))
    return _page(user, st["name"], st["name"], body, msg=msg, active="memory",
                 subtitle="记忆集与客户只读；记忆本身可增删改")


@router.post("/memory/{set_id}/customer/create")
async def memory_customer_create(
    request: Request,
    set_id: int,
    name: str = Form(""),
    aliases: str = Form(""),
):
    """只建客户、不写记忆（与管理端「新增客户」同一套规矩）。

    ① 只看规范名（名字撞上别人的别名 → 另建一位，不并进别人家）；
    ② 别名填了就以本次为准覆盖，留空不动已有；
    ③ 新建客户受 `SET_MAX_CUSTOMERS` 封顶。
    """
    user, st, deny = await _mem_set_or_deny(request, set_id)
    if deny:
        return deny
    back = f"/ext/memory/{set_id}"
    name = (name or "").strip()
    if not name or len(name) > CUSTOMER_NAME_MAX:
        return RedirectResponse(url=f"{back}?m=memname", status_code=303)
    hit = await memory_service.resolve_customer(set_id, name, name_only=True)
    if hit:
        cid = int(hit[0])
    else:
        customers = await memory_service.list_customers(set_id)
        if len(customers) >= SET_MAX_CUSTOMERS:
            logger.warning(f"外网 {user['username']} 新建客户被拒（记忆集 {set_id} 已满）")
            return RedirectResponse(url=f"{back}?m=memcustfull", status_code=303)
        cid = int(await memory_service.add_customer(set_id, name, ""))
    if (aliases or "").strip():
        await memory_service.set_aliases(set_id, cid, aliases)
    logger.info(f"🌐 外网 {user['username']} 在记忆集 {set_id} 新增/更新客户「{name}」→ #{cid}")
    return RedirectResponse(url=f"{back}?m=memcustok", status_code=303)


@router.post("/memory/{set_id}/import")
async def memory_import(
    request: Request,
    set_id: int,
    customer_name: str = Form(""),
    aliases: str = Form(""),
    text: str = Form(""),
):
    """批量导入记忆（与内网管理端同一套规矩）。

    ① 客户**只看规范名**（不看别名；名字撞上别人的别名就另建客户）；
    ② 别名**填了就以导入为准覆盖**、留空不动已有；
    ③ 事实**只追加、永不覆盖**（同客户同内容判重跳过）；
    ④ 客户与条目数上限与外网其它写入路径一致（新建客户受 `SET_MAX_CUSTOMERS` 封顶，
       条目上限由 `add_item` 兜住，超了计入"跳过"）。
    """
    user, st, deny = await _mem_set_or_deny(request, set_id)
    if deny:
        return deny
    back = f"/ext/memory/{set_id}"
    name = (customer_name or "").strip()
    customers = await memory_service.list_customers(set_id)
    if name:
        if len(name) > CUSTOMER_NAME_MAX:
            return RedirectResponse(url=f"{back}?m=memname", status_code=303)
        hit = await memory_service.resolve_customer(set_id, name, name_only=True)
        if not hit and len(customers) >= SET_MAX_CUSTOMERS:
            logger.warning(f"外网 {user['username']} 导入新建客户被拒（记忆集 {set_id} 已满）")
            return RedirectResponse(url=f"{back}?m=memcustfull", status_code=303)
    try:
        added, dup, skipped = await memory_service.import_items(
            set_id, text, customer_name=name, aliases=aliases
        )
    except ValueError as e:
        logger.info(f"外网 {user['username']} 导入被拒: {e}")
        code = "memfull" if "已满" in str(e) else "memlong"
        return RedirectResponse(url=f"{back}?m={code}", status_code=303)
    logger.info(
        f"🌐 外网 {user['username']} 导入记忆集 {set_id}（客户「{name or '通用'}」）："
        f"新增 {added} / 重复 {dup} / 跳过 {skipped}"
    )
    return RedirectResponse(
        url=f"{back}?m=memimp&a={added}&d={dup}&sk={skipped}", status_code=303
    )


@router.post("/memory/{set_id}/item/create")
async def memory_item_create(
    request: Request,
    set_id: int,
    customer_id: int = Form(0),
    content: str = Form(""),
):
    """新增**一条**记忆：`customer_id=0` 通用桶，或该记忆集里已存在的客户。

    **不在这里建客户** —— 建客户走「👤 新增客户」或「📥 批量导入」
    （单一入口，避免"随手写条记忆就多出一个客户"）。
    """
    user, st, deny = await _mem_set_or_deny(request, set_id)
    if deny:
        return deny
    back = f"/ext/memory/{set_id}"
    cid = int(customer_id or 0)
    if cid and cid not in {int(c["id"]) for c in await memory_service.list_customers(set_id)}:
        logger.warning(f"外网 {user['username']} 想给不属于记忆集 {set_id} 的客户 {cid} 写记忆")
        return RedirectResponse(url=f"{back}?m=memcust", status_code=303)
    try:
        added = await memory_service.add_item(set_id, cid, content, source="manual")
    except ValueError as e:
        logger.info(f"外网 {user['username']} 添加记忆被拒: {e}")
        code = "memempty" if "空" in str(e) else ("memfull" if "已满" in str(e) else "memlong")
        return RedirectResponse(url=f"{back}?m={code}", status_code=303)
    logger.info(f"🌐 外网 {user['username']} 在记忆集 {set_id} 添加记忆（客户 {cid}）: {added}")
    return RedirectResponse(url=f"{back}?m={'memok' if added else 'memdup'}", status_code=303)


@router.post("/memory/{set_id}/item/{item_id}/update")
async def memory_item_update(
    request: Request, set_id: int, item_id: int, content: str = Form("")
):
    user, st, deny = await _mem_set_or_deny(request, set_id)
    if deny:
        return deny
    try:
        ok = await memory_service.update_item(set_id, item_id, content)
    except ValueError as e:
        logger.info(f"外网 {user['username']} 修改记忆被拒: {e}")
        return RedirectResponse(url=f"/ext/memory/{set_id}?m=memempty", status_code=303)
    logger.info(f"🌐 外网 {user['username']} 修改记忆集 {set_id} 的记忆 #{item_id}: {ok}")
    # update_item 按 (id, set_id) 更新，改到 0 行 = 条目不存在或不属于本记忆集
    return RedirectResponse(url=f"/ext/memory/{set_id}?m={'memupd' if ok else 'memitem'}",
                            status_code=303)


@router.post("/memory/{set_id}/item/{item_id}/delete")
async def memory_item_delete(request: Request, set_id: int, item_id: int):
    user, st, deny = await _mem_set_or_deny(request, set_id)
    if deny:
        return deny
    ok = await memory_service.delete_item(set_id, item_id)
    logger.info(f"🌐 外网 {user['username']} 删除记忆集 {set_id} 的记忆 #{item_id}: {ok}")
    return RedirectResponse(url=f"/ext/memory/{set_id}?m={'memdel' if ok else 'memitem'}",
                            status_code=303)
