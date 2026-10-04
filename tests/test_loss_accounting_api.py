"""停机损失费率发布与损失核算端到端测试（需可用的 PostgreSQL）。

覆盖：
- 费率发布：版本按设备从 1 递增、整版原子、行号按 (起点,终点,原因)、
  未知设备 404、非法时间/负金额/超精度 422、同原因重叠 422（应用层 +
  排他约束）、不同原因区间允许重叠、发布后版本只读可查（列表/单个）；
- 损失核算：用修订固化归因段按费率边界切分、定点逐段计费按分四舍五入后汇总、
  未匹配费率分钟单列待定价（非零金额）、合计=逐行金额之和；
- 幂等固化：同 (班次,修订,费率版本) 重复 POST 返回同一账（200 idempotent=true），
  历史账在费率新版/归因更正后不变，新组合产生新账；
- 并发：并发发布版本号连续无缺口；并发核算同组合只固化一次；
- 拒绝：无固化明细的历史修订 422 SEGMENTS_UNAVAILABLE、未知班次/修订/费率版本 404。

运行方式：docker compose exec app pytest tests/test_loss_accounting_api.py
"""

import threading
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.main import app

TABLES = (
    "shift_loss_accounting_lines",
    "shift_loss_accountings",
    "rate_entries",
    "rate_versions",
    "manual_adjudications",
    "shift_result_segments",
    "shift_results",
    "event_versions",
    "event_sources",
    "shifts",
    "devices",
    "change_counter",
)

DEVICE = {"id": "CNC-01", "name": "加工中心1号"}
SHIFT = {
    "device_id": "CNC-01",
    "start": "2026-10-01T08:00:00+08:00",
    "end": "2026-10-01T16:00:00+08:00",
}
# 保养 10:00-11:00（60 分钟）；故障 10:30-12:00（与保养重叠 30 分钟，
# 按优先级保养胜出）→ 结算：保养 60、故障 60
E_MAINT = {
    "device_id": "CNC-01",
    "source_id": "MES-1",
    "version": 1,
    "start": "2026-10-01T10:00:00+08:00",
    "end": "2026-10-01T11:00:00+08:00",
    "reason": "maintenance",
}
E_FAIL = {
    "device_id": "CNC-01",
    "source_id": "PLC-9",
    "version": 1,
    "start": "2026-10-01T10:30:00+08:00",
    "end": "2026-10-01T12:00:00+08:00",
    "reason": "failure",
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _settled_shift(client) -> int:
    assert client.post("/devices", json=DEVICE).status_code in (200, 201)
    r = client.post("/shifts", json=SHIFT)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert client.post("/events", json=E_MAINT).status_code == 201
    assert client.post("/events", json=E_FAIL).status_code == 201
    assert client.post(f"/shifts/{sid}/settle").status_code == 200
    return sid


def _publish(client, entries, device="CNC-01"):
    return client.post(f"/devices/{device}/rate-tables", json={"entries": entries})


def _entry(sh, eh, reason, amount, day=1):
    """sh/eh 为整点小时（int）或 (时, 分) 元组，本地 +08:00。"""

    def fmt(x):
        hh, mm = (x, 0) if isinstance(x, int) else x
        return f"{hh:02d}:{mm:02d}:00"

    return {
        "start": f"2026-10-{day:02d}T{fmt(sh)}+08:00",
        "end": f"2026-10-{day:02d}T{fmt(eh)}+08:00",
        "reason": reason,
        "amount_per_minute": amount,
    }


# ---------------------------------------------------------------- 费率发布


def test_publish_incremental_versions_and_ordering(client):
    client.post("/devices", json=DEVICE)
    r1 = _publish(client, [_entry(8, 16, "failure", "1.00")])
    assert r1.status_code == 201, r1.text
    v1 = r1.json()
    assert v1["version"] == 1 and v1["entry_count"] == 1
    assert v1["entries"][0]["entry_no"] == 1
    # 费率定点输出（去掉尾随零；金额 1.00 规范化为 "1"）
    assert v1["entries"][0]["amount_per_minute"] == "1"
    # UTC 输出
    assert v1["entries"][0]["start"] == "2026-10-01T00:00:00Z"

    # 行号按 (起点, 终点, 原因) 排序，与请求顺序无关
    r2 = _publish(
        client,
        [
            _entry(12, 16, "failure", "2.00"),
            _entry(8, 12, "failure", "1.00"),
            _entry(8, 12, "maintenance", "3.00"),
        ],
    )
    assert r2.status_code == 201, r2.text
    v2 = r2.json()
    assert v2["version"] == 2
    nos = [(e["start"], e["reason"], e["entry_no"]) for e in v2["entries"]]
    # 两个 00:00Z 起点：failure 与 maintenance 按 reason 升序
    assert nos == [
        ("2026-10-01T00:00:00Z", "failure", 1),
        ("2026-10-01T00:00:00Z", "maintenance", 2),
        ("2026-10-01T04:00:00Z", "failure", 3),
    ]

    # 列表 / 单个版本查询
    listed = client.get("/devices/CNC-01/rate-tables").json()
    assert [v["version"] for v in listed] == [1, 2]
    got = client.get("/devices/CNC-01/rate-tables/2").json()
    assert got["entry_count"] == 3


def test_publish_unknown_device_404(client):
    r = _publish(client, [_entry(8, 16, "failure", "1")], device="NOPE")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_publish_validation_errors(client):
    client.post("/devices", json=DEVICE)

    # 空费率表
    r = _publish(client, [])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"

    # 负金额
    r = _publish(client, [_entry(8, 16, "failure", "-0.01")])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"

    # 超过 6 位小数
    r = _publish(client, [_entry(8, 16, "failure", "0.0000001")])
    assert r.status_code == 422

    # 非数字
    r = _publish(client, [_entry(8, 16, "failure", "abc")])
    assert r.status_code == 422

    # 布尔金额拒绝
    r = client.post(
        "/devices/CNC-01/rate-tables",
        json={"entries": [_entry(8, 16, "failure", True)]},
    )
    assert r.status_code == 422

    # 非法时间：缺时区 / 未对齐整分钟 / start≥end / 原因非法
    for bad in [
        {
            "start": "2026-10-01T08:00:00",
            "end": "2026-10-01T16:00:00+08:00",
            "reason": "failure",
            "amount_per_minute": "1",
        },
        {
            "start": "2026-10-01T08:00:30+08:00",
            "end": "2026-10-01T16:00:00+08:00",
            "reason": "failure",
            "amount_per_minute": "1",
        },
        {
            "start": "2026-10-01T16:00:00+08:00",
            "end": "2026-10-01T08:00:00+08:00",
            "reason": "failure",
            "amount_per_minute": "1",
        },
        {
            "start": "2026-10-01T08:00:00+08:00",
            "end": "2026-10-01T16:00:00+08:00",
            "reason": "nope",
            "amount_per_minute": "1",
        },
    ]:
        r = _publish(client, [bad])
        assert r.status_code == 422, r.text


def test_publish_same_reason_overlap_rejected_but_touching_ok(client):
    client.post("/devices", json=DEVICE)
    # 端点相接（半开）：允许，版本 1 成功
    r = _publish(
        client,
        [_entry(8, 12, "failure", "1"), _entry(12, 16, "failure", "2")],
    )
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 1

    # 重叠一分钟：拒绝（RATE_OVERLAP），不留半成品（版本号仍停在 1）
    r = _publish(
        client,
        [_entry(8, 12, "failure", "1"), _entry(11, 16, "failure", "2")],
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "RATE_OVERLAP"
    listed = client.get("/devices/CNC-01/rate-tables").json()
    assert [v["version"] for v in listed] == [1]

    # 不同原因重叠：允许
    r = _publish(
        client,
        [_entry(8, 16, "failure", "1"), _entry(8, 16, "maintenance", "2")],
    )
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2

    # 未知费率版本 404
    r = client.get("/devices/CNC-01/rate-tables/99")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RATE_VERSION_NOT_FOUND"


def test_integer_amount_accepted_and_normalized(client):
    client.post("/devices", json=DEVICE)
    r = _publish(client, [_entry(8, 16, "failure", 5)])  # JSON 整数
    assert r.status_code == 201, r.text
    assert r.json()["entries"][0]["amount_per_minute"] == "5"


# ---------------------------------------------------------------- 核算：基本切分


def test_account_basic_split_priced_and_total(client):
    sid = _settled_shift(client)
    # 费率 v1：保养全天 1.00；故障 10:30 前 0.50，之后 2.00
    r = _publish(
        client,
        [
            _entry(0, 23, "maintenance", "1.00"),
            _entry(0, 10, "failure", "0.50"),
            _entry(10, 23, "failure", "2.00"),
        ],
    )
    assert r.status_code == 201, r.text

    r = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 1, "rate_version": 1},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["revision"] == 1 and data["rate_version"] == 1
    assert data["idempotent"] is False

    # 固化段：保养 [10:00,11:00) 60 分钟；故障 [11:00,12:00) 60 分钟
    # 保养全部在 10:00 之后 → 保养费率 1.00（00:00-23:00 覆盖）→ 60.00
    # 故障 [11:00,12:00) 在 10:00 之后 → 2.00 → 120.00
    priced = {(l["reason"], l["start"], l["end"]): l for l in data["lines"]}
    assert data["reason_amounts"] == {
        "maintenance": "60.00",
        "failure": "120.00",
        "pending": "0.00",
    }
    assert data["total_amount"] == "180.00"
    assert data["priced_minutes"] == 120
    assert data["unpriced_minutes"] == 0
    assert data["reason_minutes"] == {"maintenance": 60, "failure": 60, "pending": 0}
    # 行号、费率区间回显
    mline = priced[("maintenance", "2026-10-01T02:00:00Z", "2026-10-01T03:00:00Z")]
    assert mline["amount"] == "60.00"
    assert mline["amount_per_minute"] == "1"
    assert mline["rate_entry_no"] >= 1
    # 合计 = 逐行金额之和
    assert sum(Decimal(l["amount"]) for l in data["lines"]) == Decimal(data["total_amount"])


def test_account_split_at_rate_boundary_within_segment(client):
    sid = _settled_shift(client)
    # 故障段 [11:00,12:00) 被费率边界 11:30 切成两段：
    _publish(
        client,
        [
            _entry(0, 11, "maintenance", "1"),
            _entry(0, 11, "failure", "1"),       # 覆盖到 11:00（故障段用不到）
            _entry((11, 0), (11, 30), "failure", "1.00"),
            _entry((11, 30), 12, "failure", "3.00"),
        ],
    )
    data = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 1, "rate_version": 1},
    ).json()
    flines = sorted(
        (l for l in data["lines"] if l["reason"] == "failure"),
        key=lambda l: l["start"],
    )
    assert [(l["start"], l["end"], l["minutes"], l["amount"]) for l in flines] == [
        ("2026-10-01T03:00:00Z", "2026-10-01T03:30:00Z", 30, "30.00"),
        ("2026-10-01T03:30:00Z", "2026-10-01T04:00:00Z", 30, "90.00"),
    ]
    assert data["total_amount"] == "180.00"  # 保养 60 + 故障 30 + 90


def test_account_half_up_rounding_per_piece(client):
    sid = _settled_shift(client)
    # 保养段 60 分钟，费率在每分钟都变（同费率 0.005）→ 每行 0.005 → 0.01，
    # 60 行各进位，合计 0.60（而不是 60*0.005=0.30 整体取整）
    entries = [_entry(0, 23, "failure", "0")]
    for h in range(10, 11):
        for m in range(0, 60):
            entries.append(
                {
                    "start": f"2026-10-01T{h:02d}:{m:02d}:00+08:00",
                    "end": f"2026-10-01T{h:02d}:{m + 1:02d}:00+08:00"
                    if m < 59
                    else "2026-10-01T11:00:00+08:00",
                    "reason": "maintenance",
                    "amount_per_minute": "0.005",
                }
            )
    _publish(client, entries)
    data = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 1, "rate_version": 1},
    ).json()
    mlines = [l for l in data["lines"] if l["reason"] == "maintenance"]
    assert len(mlines) == 60
    assert all(l["amount"] == "0.01" for l in mlines)
    assert data["reason_amounts"]["maintenance"] == "0.60"
    # 故障无费率（给的是 0 费率？这里故障给了 0.00 → 已定价零金额，非待定价）
    flines = [l for l in data["lines"] if l["reason"] == "failure"]
    assert all(l["priced"] and l["amount"] == "0.00" for l in flines)
    assert data["unpriced_minutes"] == 0


def test_account_unpriced_minutes_listed_separately(client):
    sid = _settled_shift(client)
    # 只发布保养费率：故障 60 分钟全部待定价
    _publish(client, [_entry(10, 11, "maintenance", "2.00")])
    data = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 1, "rate_version": 1},
    ).json()
    assert data["total_amount"] == "120.00"
    assert data["priced_minutes"] == 60
    assert data["unpriced_minutes"] == 60
    assert data["unpriced_intervals"] == [
        {
            "start": "2026-10-01T03:00:00Z",
            "end": "2026-10-01T04:00:00Z",
            "minutes": 60,
            "reason": "failure",
        }
    ]
    flines = [l for l in data["lines"] if l["reason"] == "failure"]
    assert len(flines) == 1
    assert flines[0]["priced"] is False
    assert flines[0]["amount_per_minute"] is None
    assert flines[0]["rate_entry_no"] is None
    assert flines[0]["amount"] == "0.00"  # 占位零；语义是未定价，合计不含它


def test_account_rate_reason_not_cross_applied(client):
    sid = _settled_shift(client)
    # 保养段时间只有故障费率覆盖 → 保养分钟全部待定价
    _publish(client, [_entry(10, 11, "failure", "9.99")])
    data = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 1, "rate_version": 1},
    ).json()
    assert data["unpriced_minutes"] == 120  # 保养 60 + 故障 60 都无同原因费率
    assert data["total_amount"] == "0.00"


# ---------------------------------------------------------------- 幂等固化与不可变历史


def test_accounting_idempotent_same_combo(client):
    sid = _settled_shift(client)
    _publish(
        client,
        [_entry(0, 23, "maintenance", "1"), _entry(0, 23, "failure", "1")],
    )
    body = {"revision": 1, "rate_version": 1}
    r1 = client.post(f"/shifts/{sid}/loss-accountings", json=body)
    assert r1.status_code == 201
    d1 = r1.json()
    r2 = client.post(f"/shifts/{sid}/loss-accountings", json=body)
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["idempotent"] is True
    assert d2["total_amount"] == d1["total_amount"]
    assert d2["lines"] == d1["lines"]

    # GET 读取同一账
    r3 = client.get(
        f"/shifts/{sid}/loss-accountings",
        params={"revision": 1, "rate_version": 1},
    )
    assert r3.status_code == 200
    assert r3.json()["total_amount"] == d1["total_amount"]


def test_new_rate_version_and_revision_create_new_accounts_history_immutable(client):
    sid = _settled_shift(client)  # 修订 1：m60 f60
    _publish(
        client,
        [_entry(0, 23, "maintenance", "1"), _entry(0, 23, "failure", "1")],
    )
    a1 = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 1, "rate_version": 1},
    ).json()
    assert a1["total_amount"] == "120.00"

    # 发布费率 v2（涨价）：历史账不变，(rev1, v2) 是新组合
    _publish(
        client,
        [_entry(0, 23, "maintenance", "2"), _entry(0, 23, "failure", "2")],
    )
    a1_again = client.get(
        f"/shifts/{sid}/loss-accountings",
        params={"revision": 1, "rate_version": 1},
    ).json()
    assert a1_again["total_amount"] == "120.00"

    a2 = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 1, "rate_version": 2},
    ).json()
    assert a2["idempotent"] is False
    assert a2["total_amount"] == "240.00"

    # 归因更正产生修订 2（故障缩短为 11:00-11:30 → f30），产生新组合
    r = client.post(
        "/events",
        json={
            "device_id": "CNC-01",
            "source_id": "PLC-9",
            "version": 2,
            "start": "2026-10-01T10:30:00+08:00",
            "end": "2026-10-01T11:30:00+08:00",
            "reason": "failure",
        },
    )
    assert r.status_code == 201
    a3 = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 2, "rate_version": 2},
    ).json()
    assert a3["revision"] == 2
    assert a3["reason_minutes"] == {"maintenance": 60, "failure": 30, "pending": 0}
    assert a3["total_amount"] == "180.00"  # 90 分钟 * 2
    # (rev1, v1) 老账原样可读
    old = client.get(
        f"/shifts/{sid}/loss-accountings",
        params={"revision": 1, "rate_version": 1},
    ).json()
    assert old["reason_minutes"]["failure"] == 60
    assert old["total_amount"] == "120.00"


def test_get_unknown_combo_404(client):
    sid = _settled_shift(client)
    _publish(client, [_entry(0, 23, "failure", "1")])
    r = client.get(
        f"/shifts/{sid}/loss-accountings",
        params={"revision": 1, "rate_version": 1},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "LOSS_ACCOUNTING_NOT_FOUND"


# ---------------------------------------------------------------- 拒绝路径


def test_account_unknown_shift_revision_rate_version(client):
    sid = _settled_shift(client)
    _publish(client, [_entry(0, 23, "failure", "1")])

    r = client.post(
        "/shifts/99999/loss-accountings",
        json={"revision": 1, "rate_version": 1},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SHIFT_NOT_FOUND"

    r = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 99, "rate_version": 1},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "REVISION_NOT_FOUND"

    r = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 1, "rate_version": 99},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RATE_VERSION_NOT_FOUND"

    # 非法参数（revision/rate_version < 1）→ 422 VALIDATION_ERROR
    r = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 0, "rate_version": 1},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_account_unsettled_shift_rejected(client):
    client.post("/devices", json=DEVICE)
    sid = client.post("/shifts", json=SHIFT).json()["id"]
    _publish(client, [_entry(0, 23, "failure", "1")])
    r = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 1, "rate_version": 1},
    )
    # 修订不存在（未结算）→ REVISION_NOT_FOUND
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "REVISION_NOT_FOUND"


def test_account_legacy_revision_without_segments_rejected(client):
    sid = _settled_shift(client)
    _publish(client, [_entry(0, 23, "failure", "1")])
    with engine.begin() as conn:
        conn.execute(text("UPDATE shift_results SET segments_available = FALSE"))
    r = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 1, "rate_version": 1},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "SEGMENTS_UNAVAILABLE"


def test_rate_version_must_belong_to_shift_device(client):
    sid = _settled_shift(client)  # CNC-01
    client.post("/devices", json={"id": "CNC-02", "name": "二号"})
    r = _publish(
        client, [_entry(0, 23, "failure", "1")], device="CNC-02"
    )
    assert r.status_code == 201
    # CNC-01 的班次不能引用 CNC-02 的费率版本
    r = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 1, "rate_version": 1},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RATE_VERSION_NOT_FOUND"


# ---------------------------------------------------------------- 不变量与裁定段


def test_line_amounts_sum_to_total_and_totals_consistent(client):
    sid = _settled_shift(client)
    _publish(
        client,
        [
            _entry(0, 23, "maintenance", "0.333"),
            _entry(8, 11, "failure", "0.167"),
            _entry(11, 23, "failure", "1.25"),
        ],
    )
    data = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": 1, "rate_version": 1},
    ).json()

    def _mins(iso_a, iso_b):
        return int(
            (datetime.fromisoformat(iso_b) - datetime.fromisoformat(iso_a)).total_seconds()
        ) // 60

    # 行金额之和 == 合计；各原因小计之和 == 合计
    assert sum(Decimal(l["amount"]) for l in data["lines"]) == Decimal(data["total_amount"])
    assert (
        Decimal(data["reason_amounts"]["maintenance"])
        + Decimal(data["reason_amounts"]["failure"])
        + Decimal(data["reason_amounts"]["pending"])
        == Decimal(data["total_amount"])
    )
    # 分钟守恒：定价 + 待定价 = 归因总数
    assert data["priced_minutes"] + data["unpriced_minutes"] == sum(
        data["reason_minutes"].values()
    )
    # 明细互不重叠、按起点排序，每行分钟数与区间一致
    prev_end = None
    for l in data["lines"]:
        assert prev_end is None or l["start"] >= prev_end
        prev_end = l["end"]
        assert l["minutes"] == _mins(l["start"], l["end"])


def test_accounting_uses_ruling_based_pending_segment(client):
    sid = _settled_shift(client)  # m60(10-11) f60(11-12)
    # 裁定 11:00-11:30 为待判 → 新修订：failure 30、pending 30
    r = client.post(
        f"/shifts/{sid}/adjudications",
        json={
            "ruling_no": "R-1",
            "start": "2026-10-01T11:00:00+08:00",
            "end": "2026-10-01T11:30:00+08:00",
            "reason": "pending",
            "note": "待判",
        },
    )
    assert r.status_code == 201, r.text
    rev = r.json()["revision"]
    _publish(
        client,
        [
            _entry(0, 23, "maintenance", "1"),
            _entry(0, 23, "failure", "2"),
            _entry(0, 23, "pending", "5"),
        ],
    )
    data = client.post(
        f"/shifts/{sid}/loss-accountings",
        json={"revision": rev, "rate_version": 1},
    ).json()
    assert data["reason_minutes"] == {"maintenance": 60, "failure": 30, "pending": 30}
    # 60*1 + 30*2 + 30*5 = 270
    assert data["total_amount"] == "270.00"
    assert data["reason_amounts"]["pending"] == "150.00"
    assert data["unpriced_minutes"] == 0


# ---------------------------------------------------------------- 并发


def test_concurrent_publish_versions_continuous(client):
    client.post("/devices", json=DEVICE)
    errors = []

    def pub(i):
        try:
            with TestClient(app) as c:
                r = c.post(
                    "/devices/CNC-01/rate-tables",
                    json={"entries": [_entry(0, 23, "failure", str(i))]},
                )
                if r.status_code != 201:
                    errors.append((i, r.status_code, r.text))
        except Exception as exc:  # noqa: BLE001
            errors.append((i, "exc", repr(exc)))

    threads = [threading.Thread(target=pub, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    versions = [v["version"] for v in client.get("/devices/CNC-01/rate-tables").json()]
    assert sorted(versions) == list(range(1, 9))


def test_concurrent_accounting_same_combo_single_row(client):
    sid = _settled_shift(client)
    _publish(
        client,
        [_entry(0, 23, "maintenance", "1"), _entry(0, 23, "failure", "1")],
    )
    statuses = []
    lock = threading.Lock()

    def run():
        db = SessionLocal()
        try:
            from app import services

            data, idempotent = services.account_shift_loss(db, sid, 1, 1)
            with lock:
                statuses.append((data["total_amount"], idempotent))
        finally:
            db.close()

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    created = [s for s in statuses if s[1] is False]
    assert len(created) == 1  # 只有一个事务真正固化
    assert len(statuses) == 6
    with engine.begin() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM shift_loss_accountings "
                "WHERE shift_id = :s AND revision = 1 AND rate_version = 1"
            ),
            {"s": sid},
        ).scalar()
    assert n == 1


def test_concurrent_publish_and_accounting_lock_full_version(client):
    """核算读到的费率版本要么不存在（404），要么是完整一版（行数与版本头一致）。"""
    sid = _settled_shift(client)
    N = 12  # 一次发布 12 行
    # 同原因区间不得重叠：三种原因各取 4 段，在班次 08:00-16:00 内整分钟
    entries = []
    for i in range(N):
        minute = i * 5
        entries.append(
            {
                "start": f"2026-10-01T08:{minute:02d}:00+08:00",
                "end": f"2026-10-01T08:{minute + 2:02d}:00+08:00",
                "reason": ["failure", "maintenance", "pending"][i % 3],
                "amount_per_minute": "1",
            }
        )

    stop = {"v": False}

    def publisher():
        for v in range(1, 11):
            r = client.post(
                "/devices/CNC-01/rate-tables", json={"entries": entries}
            )
            assert r.status_code == 201, (v, r.text)
        stop["v"] = True

    def accountant():
        seen_ok = 0
        while not stop["v"]:
            # 只对已发布的版本发起核算；每次若命中版本，行数必须完整
            for v in range(1, 11):
                r = client.post(
                    f"/shifts/{sid}/loss-accountings",
                    json={"revision": 1, "rate_version": v},
                )
                if r.status_code == 200 or r.status_code == 201:
                    seen_ok += 1
                    body = r.json()
                    # 该版本头声明的行数始终可完整读到
                    vt = client.get(f"/devices/CNC-01/rate-tables/{v}").json()
                    assert vt["entry_count"] == N
                    assert body is not None
                else:
                    assert r.status_code == 404, r.text
        return seen_ok

    tp = threading.Thread(target=publisher)
    tp.start()
    seen = accountant()  # 核算在主线程与发布线程并发进行
    tp.join()
    assert seen > 0
