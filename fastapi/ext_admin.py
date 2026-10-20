"""外网客户账号管理（运维用，在内网 X99 上执行）

    cd fastapi
    venv/bin/python ext_admin.py orgs                     # 先看组织 ID 和名字
    venv/bin/python ext_admin.py list                     # 现有外网账号
    venv/bin/python ext_admin.py add <用户名> <组织ID>     # 交互输入两次密码
    venv/bin/python ext_admin.py passwd <用户名>
    venv/bin/python ext_admin.py disable <用户名>          # 停用（保留账号，登录即拒）
    venv/bin/python ext_admin.py enable <用户名>
    venv/bin/python ext_admin.py del <用户名>

日常请用网页：「组织选择 → 点进组织 → 侧边栏 👥 成员」里就能建号/改密/启停/删除。
这个脚本保留给批量操作与应急（例如 fastapi 没在运行时需要改密）。
账号 → 组织的绑定就是 org_id 这一列，改组织归属先 del 再 add。
"""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

import aiomysql

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402
from app.services import group_service  # noqa: E402
from app.services.ext_user_service import ext_user_service  # noqa: E402


async def _pool():
    pool = await aiomysql.create_pool(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_DATABASE,
        minsize=1,
        maxsize=2,
        autocommit=True,
        charset="utf8mb4",
    )
    group_service.set_pool(pool)
    ext_user_service.set_pool(pool)
    await ext_user_service.ensure_table()
    return pool


def _ask_password() -> str:
    p1 = getpass.getpass("密码: ")
    if len(p1) < 8:
        sys.exit("❌ 密码至少 8 位")
    if p1 != getpass.getpass("再输一次: "):
        sys.exit("❌ 两次输入不一致")
    return p1


async def run(args) -> int:
    pool = await _pool()
    try:
        if args.cmd == "orgs":
            for g in await group_service.list_org_groups():
                counts = g.get("member_counts") or {}
                summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                print(f"  {g['id']:>4}  {g['name']}   {summary}")
            return 0

        if args.cmd == "list":
            users = await ext_user_service.list_users()
            if not users:
                print("（暂无外网账号）")
            for u in users:
                state = "启用" if u["enabled"] else "停用"
                print(
                    f"  {u['id']:>4}  {u['username']:<20} 组织={u['org_id']:<5} {state}"
                    f"  末次登录={u['last_login'] or '-'}"
                )
            return 0

        if args.cmd == "add":
            org = await group_service.get_org_group(int(args.org_id))
            if not org:
                sys.exit(f"❌ 组织不存在: {args.org_id}（先用 orgs 子命令查看）")
            pwd = _ask_password()
            uid = await ext_user_service.add_user(args.username, pwd, int(args.org_id))
            print(f"✅ 已创建 {args.username}（id={uid}）→ 组织 {org['id']} {org['name']}")
            return 0

        if args.cmd == "passwd":
            if await ext_user_service.set_password(args.username, _ask_password()):
                print(f"✅ 已改密 {args.username}")
                return 0
            sys.exit(f"❌ 账号不存在: {args.username}")

        if args.cmd in ("enable", "disable"):
            on = args.cmd == "enable"
            if await ext_user_service.set_enabled(args.username, on):
                print(f"✅ 已{'启用' if on else '停用'} {args.username}")
                return 0
            sys.exit(f"❌ 账号不存在: {args.username}")

        if args.cmd == "del":
            if await ext_user_service.delete_user(args.username):
                print(f"✅ 已删除 {args.username}")
                return 0
            sys.exit(f"❌ 账号不存在: {args.username}")
    finally:
        pool.close()
        await pool.wait_closed()
    return 0


def main():
    ap = argparse.ArgumentParser(description="外网客户账号管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("orgs", help="列出组织（取 org_id 用）")
    sub.add_parser("list", help="列出外网账号")
    p = sub.add_parser("add", help="新建账号")
    p.add_argument("username")
    p.add_argument("org_id", help="组织 ID，用 orgs 子命令查看")
    p = sub.add_parser("passwd", help="改密码")
    p.add_argument("username")
    for c in ("enable", "disable", "del"):
        p = sub.add_parser(c)
        p.add_argument("username")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
