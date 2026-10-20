"""访问日志噪音过滤：Chrome DevTools 的 well-known 探测不该刷屏。

背景：Chrome 会请求 /.well-known/appspecific/com.chrome.devtools.json 做工作区自动发现，
开着 DevTools 时每次页面刷新都会在 uvicorn 访问日志里留一条 404，看起来像报错。
这里断言它被过滤器丢掉，同时确认正常请求的日志没被误伤。
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import _QuietPathFilter  # noqa: E402

f = _QuietPathFilter()


def _record(request_line: str, status: int) -> logging.LogRecord:
    """按 uvicorn 的真实格式构造访问日志记录（5 个占位符）。"""
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s" %d',
        ("127.0.0.1:39232", request_line, status),
        None,
    )


def test_chrome_devtools_probe_is_dropped():
    rec = _record("GET /.well-known/appspecific/com.chrome.devtools.json HTTP/1.1", 404)
    assert f.filter(rec) is False


def test_normal_requests_keep_their_logs():
    for line, status in (
        ("GET /ext/devices HTTP/1.1", 200),
        ("POST /ext/login HTTP/1.1", 303),
        ("GET /admin/orgs HTTP/1.1", 200),
        ("GET /favicon.ico HTTP/1.1", 404),
    ):
        assert f.filter(_record(line, status)) is True, line


def test_broken_record_is_not_swallowed():
    """异常记录宁可放行也别静默丢弃 —— 过滤器不该吃掉自己的 bug。"""
    bad = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s %s", ("only-one",), None)
    assert f.filter(bad) is True
