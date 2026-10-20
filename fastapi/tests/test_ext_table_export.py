"""外网客户控制台「表格筛选 + 下载 CSV」的纯逻辑自检（不连数据库）。

回归背景：导出是 /ext 上唯一能一次性把整表带出系统的口子，筛选条件一旦解析错，
客户拿到的就是超范围的数据；CSV 里的公式前缀又是典型的 Excel 注入面。
筛选还必须按字段类型走（文本可模糊、数字/时间可比大小），否则客户只能「等于」。
"""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.routes.ext import (  # noqa: E402
    MAX_FILTER_ROWS,
    _cell,
    _csv_headers,
    _csv_text,
    _field_type,
    _filter_rows,
    _table_where,
    _type_ops,
    _where_from_rows,
    ext_tunnel_guard,
    router as ext_router,
)

TBL = {
    "name": "订单/明细",
    "table_key": "orders",
    "fields": [
        {"name": "a", "label": "甲", "type": "varchar"},
        {"name": "n", "type": "int"},
        {"name": "d", "type": "datetime"},
    ],
}


def test_no_filter_or_empty_value():
    assert _table_where(TBL, "", "", "") == (None, "")
    # 选了字段但没填值 = 不筛选（而不是 field = '' 这种看不懂的结果）
    assert _table_where(TBL, "a", "=", "  ") == (None, "")


def test_unknown_field_and_wrong_op_rejected():
    where, err = _table_where(TBL, "nope", "=", "1")
    assert where is None and "字段不存在" in err
    where, err = _table_where(TBL, "n", "like", "1")
    assert where is None and "不适用" in err


def test_text_ops_equal_and_contains():
    assert _table_where(TBL, "a", "=", "x") == ([{"field": "a", "op": "=", "value": "x"}], "")
    # like 自动补 %：客户不必懂 SQL 通配符；已带 % 的不重复包
    assert _table_where(TBL, "a", "like", "x")[0][0]["value"] == "%x%"
    assert _table_where(TBL, "a", "like", "%x")[0][0]["value"] == "%x"


def test_number_ops_and_type_guard():
    assert _table_where(TBL, "n", ">=", "5") == ([{"field": "n", "op": ">=", "value": 5}], "")
    where, err = _table_where(TBL, "n", ">", "abc")
    assert where is None and "数字" in err
    where, err = _table_where(TBL, "n", "<", "5.5")
    assert where is None and "整数" in err


def test_datetime_filters_are_whole_day():
    # 等于某天 = 当天区间（否则恒等于 00:00:00，客户永远查不到）
    assert _table_where(TBL, "d", "=", "2024-03-05")[0] == [
        {"field": "d", "op": ">=", "value": "2024-03-05"},
        {"field": "d", "op": "<", "value": "2024-03-06"},
    ]
    # 晚于某天 = 次日 0 点起；不晚于某天 = 次日 0 点前
    assert _table_where(TBL, "d", ">", "2024-03-05")[0] == [
        {"field": "d", "op": ">=", "value": "2024-03-06"}
    ]
    assert _table_where(TBL, "d", "<=", "2024-03-05")[0] == [
        {"field": "d", "op": "<", "value": "2024-03-06"}
    ]
    assert _table_where(TBL, "d", ">=", "2024-03-05")[0] == [
        {"field": "d", "op": ">=", "value": "2024-03-05"}
    ]
    # 非日期值不猜，退回精确比较
    assert _table_where(TBL, "d", "=", "garbage")[0] == [{"field": "d", "op": "=", "value": "garbage"}]


def test_ops_follow_field_type():
    assert "like" not in {o for o, _ in _type_ops(_field_type(TBL, "n"))}
    assert ">" not in {o for o, _ in _type_ops(_field_type(TBL, "a"))}
    assert _type_ops(None)  # 未知/未选字段有兜底，不抛异常


def test_filter_rows_add_del_and_legacy():
    p = {
        "n": "2",
        "f0_field": "a", "f0_op": "like", "f0_value": "x",
        "f1_field": "n", "f1_op": ">", "f1_value": "1",
    }
    assert [r["field"] for r in _filter_rows(p)] == ["a", "n"]
    assert [r["field"] for r in _filter_rows({**p, "add": "1"})] == ["a", "n", ""]
    assert [r["field"] for r in _filter_rows({**p, "del": "0"})] == ["n"]
    # 上限：点不出第 MAX_FILTER_ROWS+1 行
    many = {"n": str(MAX_FILTER_ROWS), "add": "1",
            **{f"f{i}_field": "a" for i in range(MAX_FILTER_ROWS)}}
    assert len(_filter_rows(many)) == MAX_FILTER_ROWS
    # 旧链接/书签的 field/op/value 仍当第一条
    assert _filter_rows({"field": "a", "op": "=", "value": "1"})[0]["field"] == "a"
    # 新页面永远是 f0_/f1_…，旧参数只在第一条为空时兜底
    assert _filter_rows({"field": "a", "n": "2", "f0_field": "n"})[0]["field"] == "n"


def test_multiple_conditions_are_anded():
    where, errs, _ = _where_from_rows(
        TBL,
        [
            {"field": "n", "op": ">", "value": "1"},
            {"field": "n", "op": "<", "value": "10"},
            {"field": "a", "op": "like", "value": "x"},
        ],
    )
    assert errs == []
    assert where == [
        {"field": "n", "op": ">", "value": 1},
        {"field": "n", "op": "<", "value": 10},
        {"field": "a", "op": "like", "value": "%x%"},
    ]


def test_blank_rows_ignored_and_op_mismatch_lenient_vs_strict():
    rows = [{"field": "", "op": "", "value": ""}, {"field": "a", "op": "=", "value": "  "}]
    assert _where_from_rows(TBL, rows)[:2] == ([], [])
    # 页面：运算符与类型不符 → 静默回到「等于」，并把真正生效的值回写给表单
    _, errs, fixed = _where_from_rows(TBL, [{"field": "n", "op": "like", "value": "1"}])
    assert errs == [] and fixed[0]["op"] == "="
    # 下载：同一个错误直接报出来，不猜
    _, errs, _ = _where_from_rows(
        TBL, [{"field": "n", "op": "like", "value": "1"}], strict=True
    )
    assert errs and "不适用" in errs[0]


def test_csv_text_header_labels_bom_and_escaping():
    out = _csv_text(["a", "b"], {"a": "甲", "b": "b"}, [{"a": "x,y", "b": 'say "hi"'}])
    assert out.startswith("\ufeff甲,b\r\n")
    assert '"x,y"' in out and '"say ""hi"""' in out


def test_cell_guards_formula_and_json():
    assert _cell("=1+1") == "'=1+1"
    assert _cell("+86") == "'+86"
    assert _cell(None) == ""
    assert _cell({"k": "中"}) == '{"k": "中"}'


def test_csv_filename_falls_back_and_encodes():
    assert "table.csv" in _csv_headers({"name": ""})["Content-Disposition"]
    cd = _csv_headers(TBL)["Content-Disposition"]
    assert cd.split("filename*=UTF-8''")[1] == "%E8%AE%A2%E5%8D%95%E6%98%8E%E7%BB%86.csv"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "EXT_TUNNEL_SECRET", "s3cret", raising=False)
    app = FastAPI()
    app.middleware("http")(ext_tunnel_guard)
    app.include_router(ext_router)
    c = TestClient(app)
    c.headers["X-Tunnel-Secret"] = "s3cret"
    return c


def test_export_route_registered():
    assert "/ext/table/{table_key}/export" in {r.path for r in ext_router.routes}


def test_export_requires_session_before_touching_db(client):
    """未登录时必须在碰数据库之前就被弹去登录页（越权面在鉴权，不在查询）。"""
    r = client.get("/ext/table/orders/export", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ext/login"
