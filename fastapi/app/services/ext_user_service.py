"""
外网客户账号（/ext 专用）

只承担两件事：
  1. 账号 → 组织（org_groups.id）的绑定与密码校验 —— 标准库 hashlib.scrypt，零新依赖
  2. 会话 cookie 的签名/校验 —— HMAC-SHA256

刻意不做的事：自助注册、多租户、RBAC 框架、密码找回。
十几个固定账号由运维在内网手工建号/改密即可，不值得为它长出一套用户体系。
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Dict, List, Optional, Tuple

import aiomysql

from ..config import settings

logger = logging.getLogger(__name__)

# scrypt 参数（N=2^14 约 16MB 内存/次，浏览器端登录场景足够）
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

SESSION_COOKIE = "ext_session"
_DEFAULT_SESSION_HOURS = 12


def session_ttl() -> int:
    """会话有效期（秒）。来自配置 EXT_SESSION_HOURS；非法值退回 12 小时。

    到期即失效，但客户有活动时会续期（见 routes/ext.py 的 ext_session_middleware），
    所以实际体感是"一直在用就不会掉线，闲置超过 TTL 才需要重登"。
    """
    try:
        hours = int(settings.EXT_SESSION_HOURS)
    except Exception:
        hours = _DEFAULT_SESSION_HOURS
    return max(1, hours) * 3600


# ── 密码 ──────────────────────────────────────────────


def hash_password(password: str, salt: str = "") -> Tuple[str, str]:
    """返回 (salt, hex_hash)。salt 为空时随机生成。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt.encode("utf-8"),
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
    )
    return salt, dk.hex()


def verify_password(password: str, salt: str, expected: str) -> bool:
    """定长比较，避免时序侧信道。"""
    if not salt or not expected:
        return False
    try:
        _, got = hash_password(password, salt)
    except Exception:
        return False
    return hmac.compare_digest(got, expected)


# ── 会话 cookie ───────────────────────────────────────


def session_fp(pwd_hash: str) -> str:
    """会话指纹：只暴露哈希的指纹，用来把会话绑到"签发时的那个密码"。

    改密后指纹变化 → 所有旧会话（含别人机器上的）自动失效。
    """
    return hashlib.sha256((pwd_hash or "").encode("utf-8")).hexdigest()[:16]


def session_key(secret: str) -> bytes:
    """从隧道密钥派生会话签名密钥：只配一个密钥，少一处运维负担。"""
    return hmac.new(
        (secret or "").encode("utf-8"), b"aivbot-ext-session", hashlib.sha256
    ).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_session(
    user_id: int, org_id: int, key: bytes, fp: str = "", ttl: Optional[int] = None
) -> str:
    if ttl is None:
        ttl = session_ttl()
    payload = json.dumps(
        {"u": int(user_id), "o": int(org_id), "e": int(time.time()) + int(ttl), "f": fp or ""},
        separators=(",", ":"),
    )
    body = _b64e(payload.encode("utf-8"))
    sig = _b64e(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def parse_session(token: str, key: bytes) -> Optional[Dict]:
    """校验签名与过期时间，返回 {"u","o","e"}；任何异常一律当无效。"""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expect = _b64e(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        data = json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("u") or not data.get("o"):
        return None
    if int(data.get("e", 0)) < int(time.time()):
        return None
    return data


# ── 账号表 ────────────────────────────────────────────


class ExtUserService:
    """ext_users：外网客户账号"""

    def __init__(self):
        self._pool: Optional[aiomysql.Pool] = None

    def set_pool(self, pool: aiomysql.Pool):
        self._pool = pool

    async def ensure_table(self):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ext_users (
                        id         INT AUTO_INCREMENT PRIMARY KEY,
                        org_id     INT          NOT NULL,
                        username   VARCHAR(64)  NOT NULL,
                        pwd_salt   VARCHAR(32)  NOT NULL,
                        pwd_hash   VARCHAR(128) NOT NULL,
                        enabled    TINYINT      NOT NULL DEFAULT 1,
                        created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_login DATETIME     NULL,
                        UNIQUE KEY uk_ext_username (username),
                        KEY idx_ext_org (org_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                # 账号级可见范围：组织决定"拥有什么"，这张表决定"这个账号能看其中哪些"。
                # 一条记录都没有 = 跟随组织（全可见，兼容未配置的账号）；
                # 一旦有任何记录，就完全以本表为准（没记录的类别 → 看不见），失败关闭。
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ext_user_scope (
                        user_id   INT          NOT NULL,
                        kind      VARCHAR(16)  NOT NULL,
                        member_id VARCHAR(64)  NOT NULL,
                        PRIMARY KEY (user_id, kind, member_id),
                        KEY idx_scope_user (user_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                await conn.commit()
        logger.info("✅ ext_users / ext_user_scope tables ensured")

    # ── 账号级可见范围 ───────────────────────────────

    async def get_scope(self, user_id: int) -> Dict[str, List[str]]:
        """返回 {kind: [member_id]}。空 dict = 从未配置过（跟随组织全可见）。"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT kind, member_id FROM ext_user_scope WHERE user_id=%s",
                    (int(user_id),),
                )
                rows = await cur.fetchall()
        scope: Dict[str, List[str]] = {}
        for kind, mid in rows:
            scope.setdefault(str(kind), []).append(str(mid))
        return scope

    async def count_scope(self, user_id: int) -> int:
        """该账号是否已配置过范围（0 = 跟随组织）。"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM ext_user_scope WHERE user_id=%s", (int(user_id),)
                )
                row = await cur.fetchone()
        return int(row[0] or 0) if row else 0

    async def set_scope(self, user_id: int, mapping: Dict[str, List[str]]):
        """整体覆盖。mapping 中缺省的类别 = 该类别不可见（调用方应传全部类别）。"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM ext_user_scope WHERE user_id=%s", (int(user_id),)
                )
                for kind, ids in mapping.items():
                    for mid in ids:
                        await cur.execute(
                            """INSERT IGNORE INTO ext_user_scope (user_id, kind, member_id)
                               VALUES (%s,%s,%s)""",
                            (int(user_id), str(kind), str(mid)),
                        )
                await conn.commit()
        logger.info(
            f"✅ 外网账号 {user_id} 可见范围已更新: "
            + " ".join(f"{k}={len(v)}" for k, v in sorted(mapping.items()))
        )

    # ── 管理（内网手工调用）──────────────────────────

    async def add_user(self, username: str, password: str, org_id: int) -> int:
        """建号。用户名重复时报错，不静默改密。"""
        username = (username or "").strip()
        if not username:
            raise ValueError("用户名不能为空")
        if not password:
            raise ValueError("密码不能为空")
        salt, pwd_hash = hash_password(password)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT id FROM ext_users WHERE username=%s", (username,))
                if await cur.fetchone():
                    raise ValueError(f"账号已存在: {username}")
                await cur.execute(
                    """INSERT INTO ext_users (org_id, username, pwd_salt, pwd_hash)
                       VALUES (%s, %s, %s, %s)""",
                    (int(org_id), username, salt, pwd_hash),
                )
                new_id = cur.lastrowid
                await conn.commit()
        logger.info(f"✅ 新建外网账号: {username} → 组织 {org_id}")
        return int(new_id)

    async def set_password(self, username: str, password: str) -> bool:
        salt, pwd_hash = hash_password(password)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE ext_users SET pwd_salt=%s, pwd_hash=%s WHERE username=%s",
                    (salt, pwd_hash, (username or "").strip()),
                )
                ok = cur.rowcount > 0
                await conn.commit()
        return ok

    async def set_enabled(self, username: str, enabled: bool) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE ext_users SET enabled=%s WHERE username=%s",
                    (1 if enabled else 0, (username or "").strip()),
                )
                ok = cur.rowcount > 0
                await conn.commit()
        return ok

    async def delete_user(self, username: str) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM ext_users WHERE username=%s", ((username or "").strip(),)
                )
                row = await cur.fetchone()
                if not row:
                    return False
                # 顺手清掉范围记录，避免 id 复用后继承旧范围
                await cur.execute("DELETE FROM ext_user_scope WHERE user_id=%s", (row[0],))
                await cur.execute("DELETE FROM ext_users WHERE id=%s", (row[0],))
                await conn.commit()
        return True

    async def list_users(self, org_id: int = 0) -> List[dict]:
        """org_id>0 时只列该组织的账号（组织管理页用）。"""
        sql = """SELECT id, org_id, username, enabled, created_at, last_login
                 FROM ext_users"""
        params: tuple = ()
        if org_id:
            sql += " WHERE org_id=%s"
            params = (int(org_id),)
        sql += " ORDER BY id ASC"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "org_id": r[1],
                "username": r[2],
                "enabled": bool(r[3]),
                "created_at": r[4].isoformat() if r[4] else "",
                "last_login": r[5].isoformat() if r[5] else "",
            }
            for r in rows
        ]

    # ── 登录使用 ─────────────────────────────────────

    async def get(self, user_id: int) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, org_id, username, enabled, pwd_hash FROM ext_users WHERE id=%s",
                    (int(user_id),),
                )
                row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "org_id": row[1],
            "username": row[2],
            "enabled": bool(row[3]),
            "pwd_fp": session_fp(row[4]),
        }

    async def get_auth(self, user_id: int) -> Optional[dict]:
        """改密用：取盐与哈希（只在服务端比对，不返回给页面）。"""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, username, pwd_salt, pwd_hash FROM ext_users WHERE id=%s",
                    (int(user_id),),
                )
                row = await cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "username": row[1], "pwd_salt": row[2], "pwd_hash": row[3]}

    async def authenticate(self, username: str, password: str) -> Optional[dict]:
        """校验账号密码。成功返回用户 dict，失败返回 None（不区分原因）。"""
        username = (username or "").strip()
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT id, org_id, username, enabled, pwd_salt, pwd_hash
                       FROM ext_users WHERE username=%s""",
                    (username,),
                )
                row = await cur.fetchone()
        if not row or not bool(row[3]):
            # 账号不存在/被停用也走一次 hash，避免用响应时间区分账号是否存在
            hash_password(password or "x")
            return None
        if not verify_password(password or "", row[4], row[5]):
            return None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE ext_users SET last_login=NOW() WHERE id=%s", (row[0],)
                )
                await conn.commit()
        return {
            "id": row[0],
            "org_id": row[1],
            "username": row[2],
            "enabled": True,
            "pwd_fp": session_fp(row[5]),
        }


# 全局服务实例（pool 在 startup 时注入）
ext_user_service = ExtUserService()
