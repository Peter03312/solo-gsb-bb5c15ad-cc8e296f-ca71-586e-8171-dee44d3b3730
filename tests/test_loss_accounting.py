"""停机损失费率发布与损失核算（POST /devices/{id}/rate-versions、
POST /shifts/{id}/loss-accountings）端到端测试。

覆盖：完整费率版本原子发布与版本号递增、非法/重叠/空费率表拒绝、
未知对象拒绝、按费率边界切分、逐段定点四舍五入到分后汇总、未匹配
分钟单列待定价（不作零金额）、(班次, 修订, 费率版本) 幂等固化、
历史账不可改写、无固化明细历史修订拒绝、并发发布版本连续与并发核算唯一。

运行方式：docker compose exec app pytest tests/test_loss_accounting.py
"""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "loss_accounting_pieces",
    "loss_accountings",
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


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


# ---------------------------------------------------------------- 构造数据


def _settled_shift_with_two_reasons(client, device="CNC-01") -> int:
    """已结算班次 10/1 08:00-16:00：故障 10:00-12:00 + 保养 11:00-11:30。

    固化明细：failure 10:00-11:00、maintenance 11:00-11:30、failure 11:30-12:00。
    """
    client.post("/devices", json={**DEVICE, "id": device})
    r = client.post(
        "/shifts",
        json={
            "device_id": device,
            "start": "2026-10-01T08:00:00+08:00",
            "end": "2026-10-01T16:00:00+08:00",
        },
    )
    assert r.status_code == 201, r.text
    shift_id = r.json()["id"]
    for body in (
        {
            "device_id": device,
            "source_id": "PLC-9",
            "version": 1,
            "start": "2026-10-01T10:00:00+08:00",
            "end": "2026-10-01T12:00:00+08:00",
            "reason": "failure",
        },
        {
            "device_id": device,
            "source_id": "MES-1",
            "version": 1,
            "start": "2026-10-01T11:00:00+08:00",
            "end": "2026-10-01T11:30:00+08:00",
            "reason": "maintenance",
        },
    ):
        rr = client.post("/events", json=body)
        assert rr.status_code == 201, rr.text
    sr = client.post(f"/shifts/{shift_id}/settle")
    assert sr.status_code == 200, sr.text
    return shift_id


def _entry(reason, sh, eh, amount):
    return {
        "reason": reason,
        "start": f"2026-10-01T{sh}+08:00",
        "end": f"2026-10-01T{eh}+08:00",
        "amount_per_minute": amount,
    }


def _publish(client, entries, device="CNC-01", note="", expect=201):
    r = client.post(
        f"/devices/{device}/rate-versions",
        json={"entries": entries, "note": note},
    )
    assert r.status_code == expect, r.text
    return r


def _account(client, shift_id, body, expect=201):
    r = client.post(f"/shifts/{shift_id}/loss-accountings", json=body)
    assert r.status_code == expect, r.text
    return r.json()


# ---------------------------------------------------------------- 费率发布


def test_publish_rate_versions_incrementing_and_atomic_readback(client):
    client.post("/devices", json=DEVICE)
    r1 = _publish(
        client,
        [
            _entry("failure", "00:00:00", "12:00:00", "10"),
            _entry("maintenance", "00:00:00", "12:00:00", "5"),
        ],
        note="v1",
    ).json()
    assert r1["version"] == 1 and r1["entry_count"] == 2
    assert [e["entry_no"] for e in r1["entries"]] == [1, 2]

    r2 = _publish(client, [_entry("failure", "00:00:00", "23:59:00", "12")]).json()
    assert r2["version"] == 2 and r2["entry_count"] == 1

    # 列表：最新在前，entry_count 正确
    listing = client.get("/devices/CNC-01/rate-versions").json()
    assert [v["version"] for v in listing] == [2, 1]
    assert [v["entry_count"] for v in listing] == [1, 2]

    # 读取版本 1 的完整内容（历史版本不随后续发布改变）
    got = client.get("/devices/CNC-01/rate-versions/1").json()
    assert {(e["reason"], e["amount_per_minute"]) for e in got["entries"]} == {
        ("failure", "10"),
        ("maintenance", "5"),
    }


def test_publish_rate_version_boundary_touching_allowed(client):
    client.post("/devices", json=DEVICE)
    # 同原因端点相接不算重叠
    r = _publish(
        client,
        [
            _entry("failure", "10:00:00", "11:00:00", "10"),
            _entry("failure", "11:00:00", "12:00:00", "20"),
        ],
    )
    assert r.json()["version"] == 1


def test_publish_rejects_overlap_empty_bad_amount_and_ranges(client):
    client.post("/devices", json=DEVICE)

    def _code(entries):
        r = client.post(
            "/devices/CNC-01/rate-versions", json={"entries": entries}
        )
        assert r.status_code == 422, r.text
        return r.json()["detail"]["code"]

    assert _code([]) == "VALIDATION_ERROR"  # 空表（pydantic min_length）
    # 同原因重叠
    assert _code(
        [
            _entry("failure", "10:00:00", "12:00:00", "10"),
            _entry("failure", "11:00:00", "13:00:00", "20"),
        ]
    ) == "RATE_ENTRY_INVALID"
    # 负金额 / 非数字 / 超过 6 位小数
    for bad in ("-1", "abc", "1.0000001"):
        assert _code([_entry("failure", "10:00:00", "11:00:00", bad)]) == (
            "RATE_ENTRY_INVALID"
        )
    # 区间倒置 / 缺时区 / 未对齐整分钟 → 统一 422
    for bad_entry in (
        {"reason": "failure", "start": "2026-10-01T12:00:00+08:00",
         "end": "2026-10-01T10:00:00+08:00", "amount_per_minute": "1"},
        {"reason": "failure", "start": "2026-10-01T10:00:00",
         "end": "2026-10-01T11:00:00", "amount_per_minute": "1"},
        {"reason": "failure", "start": "2026-10-01T10:00:30+08:00",
         "end": "2026-10-01T11:00:00+08:00", "amount_per_minute": "1"},
    ):
        r = client.post(
            "/devices/CNC-01/rate-versions", json={"entries": [bad_entry]}
        )
        assert r.status_code == 422, r.text

    # 全部拒绝后无任何版本残留（原子性）
    assert client.get("/devices/CNC-01/rate-versions").json() == []


def test_publish_rate_version_unknown_device_404(client):
    r = client.post(
        "/devices/NOPE/rate-versions",
        json={"entries": [_entry("failure", "10:00:00", "11:00:00", "1")]},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_get_unknown_rate_version_404(client):
    client.post("/devices", json=DEVICE)
    r = client.get("/devices/CNC-01/rate-versions/99")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RATE_VERSION_NOT_FOUND"


def test_concurrent_publishes_get_contiguous_versions(client):
    client.post("/devices", json=DEVICE)
    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait(timeout=10)
            r = client.post(
                "/devices/CNC-01/rate-versions",
                json={"entries": [_entry("failure", "00:00:00", "23:59:00", "1")]},
            )
            assert r.status_code == 201, r.text
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    versions = [v["version"] for v in client.get("/devices/CNC-01/rate-versions").json()]
    assert sorted(versions) == [1, 2, 3, 4]


# ---------------------------------------------------------------- 核算


def test_accounting_basic_split_totals_and_unpriced(client):
    shift_id = _settled_shift_with_two_reasons(client)
    # v1：故障全天 1 元/分；无保养费率
    _publish(client, [_entry("failure", "00:00:00", "23:59:00", "1")])

    data = _account(client, shift_id, {"rate_version": 1})
    assert data["revision"] == 1 and data["rate_version"] == 1
    # 定价：故障 90 分钟（10-11、11:30-12:00）× 1 元 = 90.00
    assert data["totals"]["amount"] == "90.00"
    assert data["totals"]["amount_cents"] == 9000
    assert data["totals"]["priced_minutes"] == 90
    # 保养 30 分钟无费率 → 待定价，绝不按零金额
    assert data["totals"]["unpriced_minutes"] == 30
    assert data["totals"]["amount_cents"] == 9000

    priced = data["priced_pieces"]
    assert [(p["seg_no"], p["minutes"], p["reason"], p["amount"]) for p in priced] == [
        (1, 60, "failure", "60.00"),
        (3, 30, "failure", "30.00"),
    ]
    unpriced = data["unpriced_pieces"]
    assert len(unpriced) == 1
    assert unpriced[0]["seg_no"] == 2
    assert unpriced[0]["reason"] == "maintenance"
    assert unpriced[0]["minutes"] == 30
    assert unpriced[0]["start"] == "2026-10-01T03:00:00Z"
    assert unpriced[0]["end"] == "2026-10-01T03:30:00Z"

    by = {r["reason"]: r for r in data["totals"]["by_reason"]}
    assert by["failure"] == {
        "reason": "failure",
        "priced_minutes": 90,
        "unpriced_minutes": 0,
        "amount": "90.00",
        "amount_cents": 9000,
    }
    assert by["maintenance"]["unpriced_minutes"] == 30
    assert by["maintenance"]["amount"] == "0.00"
    assert by["pending"]["priced_minutes"] == 0


def test_accounting_split_at_rate_boundary_with_half_up_per_piece(client):
    shift_id = _settled_shift_with_two_reasons(client)
    # 故障费率按分钟边界：10:00-11:00 每分钟 0.005 元（60 段各 0.5 分→1 分），
    # 11:30-12:00 每分钟 0.005 元（30 段各 1 分）。
    # 固化段 1 与 3 内部均无费率边界之外的拆分（逐分钟费率相同但边界存在）：
    # 用同费率的整分钟相邻费率行验证“按费率边界切分 + 逐段取整”。
    entries = []
    for minute in range(10 * 60, 12 * 60):  # 10:00-12:00 逐分钟费率行
        hh, mm = divmod(minute, 60)
        entries.append(
            _entry(
                "failure",
                f"{hh:02d}:{mm:02d}:00",
                f"{hh:02d}:{mm + 1:02d}:00" if mm < 59 else f"{hh + 1:02d}:00:00",
                "0.005",
            )
        )
    _publish(client, entries)
    data = _account(client, shift_id, {"rate_version": 1})
    # 故障 90 个一分钟段，各段 0.5 分向上到 1 分 → 合计 90 分（不是 45 分）
    assert data["totals"]["amount"] == "0.90"
    assert data["totals"]["amount_cents"] == 90
    assert data["totals"]["priced_minutes"] == 90
    # 每段一分钟、金额 0.01
    assert all(p["minutes"] == 1 and p["amount"] == "0.01" for p in data["priced_pieces"])
    assert len(data["priced_pieces"]) == 90
    # 保养 30 分钟仍待定价
    assert data["totals"]["unpriced_minutes"] == 30


def test_accounting_zero_rate_is_priced_not_unpriced(client):
    shift_id = _settled_shift_with_two_reasons(client)
    _publish(
        client,
        [
            _entry("failure", "00:00:00", "23:59:00", "0"),
            _entry("maintenance", "00:00:00", "23:59:00", "0"),
        ],
    )
    data = _account(client, shift_id, {"rate_version": 1})
    assert data["totals"]["unpriced_minutes"] == 0
    assert data["totals"]["priced_minutes"] == 120
    assert data["totals"]["amount"] == "0.00"
    assert all(p["amount"] == "0.00" for p in data["priced_pieces"])
    assert data["unpriced_pieces"] == []


def test_accounting_unpriced_hole_between_rates(client):
    shift_id = _settled_shift_with_two_reasons(client)
    # 故障费率只覆盖 10:00-10:30 与 11:30-12:00：段 1 的 10:30-11:00 为中间缺口
    _publish(
        client,
        [
            _entry("failure", "10:00:00", "10:30:00", "2"),
            _entry("failure", "11:30:00", "12:00:00", "4"),
            _entry("maintenance", "11:00:00", "11:30:00", "1"),
        ],
    )
    data = _account(client, shift_id, {"rate_version": 1})
    # 30×2 + 30×4 + 30×1 = 60+120+30 = 210
    assert data["totals"]["amount"] == "210.00"
    assert data["totals"]["priced_minutes"] == 90
    assert data["totals"]["unpriced_minutes"] == 30
    unpriced = data["unpriced_pieces"]
    assert [(u["seg_no"], u["start"], u["end"], u["minutes"]) for u in unpriced] == [
        (1, "2026-10-01T02:30:00Z", "2026-10-01T03:00:00Z", 30)
    ]


def test_accounting_idempotent_and_history_immutable(client):
    shift_id = _settled_shift_with_two_reasons(client)
    _publish(client, [_entry("failure", "00:00:00", "23:59:00", "1")])

    first = _account(client, shift_id, {"rate_version": 1}, expect=201)
    assert first["idempotent"] is False
    created_at = first["created_at"]
    # 同组合重复核算 → 200 幂等，内容逐字节一致
    second = _account(client, shift_id, {"rate_version": 1}, expect=200)
    assert second["idempotent"] is True
    assert second["created_at"] == created_at
    assert second["totals"] == first["totals"]
    assert second["priced_pieces"] == first["priced_pieces"]

    # 库里同组合只有一个账头
    with engine.begin() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM loss_accountings "
                "WHERE shift_id=:s AND revision=1 AND rate_version=1"
            ),
            {"s": shift_id},
        ).scalar()
    assert n == 1

    # 发布新版本只产生新组合的账，旧账内容不变
    _publish(client, [_entry("failure", "00:00:00", "23:59:00", "2")])
    new = _account(client, shift_id, {"rate_version": 2}, expect=201)
    assert new["idempotent"] is False
    old = _account(client, shift_id, {"rate_version": 1}, expect=200)
    assert old["totals"]["amount"] == "90.00"
    assert new["totals"]["amount"] == "180.00"


def test_new_revision_creates_new_combination_old_account_untouched(client):
    shift_id = _settled_shift_with_two_reasons(client)
    _publish(client, [_entry("failure", "00:00:00", "23:59:00", "1")])
    rev1 = _account(client, shift_id, {"rate_version": 1}, expect=201)

    # 归因更正：故障缩短为 10:00-11:00 → 修订 2（故障 60、保养 30）
    r = client.post(
        "/events",
        json={
            "device_id": "CNC-01",
            "source_id": "PLC-9",
            "version": 2,
            "start": "2026-10-01T10:00:00+08:00",
            "end": "2026-10-01T11:00:00+08:00",
            "reason": "failure",
        },
    )
    assert r.status_code == 201, r.text

    rev2_latest = _account(client, shift_id, {"rate_version": 1}, expect=201)
    assert rev2_latest["revision"] == 2
    assert rev2_latest["totals"]["amount"] == "60.00"
    assert rev2_latest["totals"]["priced_minutes"] == 60
    # 修订 1 × 费率 1 的历史账仍可按显式 revision 取回且内容不变
    rev1_again = _account(
        client, shift_id, {"rate_version": 1, "revision": 1}, expect=200
    )
    assert rev1_again["totals"] == rev1["totals"]
    assert rev1_again["revision"] == 1

    # 列表含两个组合，按费率版本、修订升序
    listing = client.get(f"/shifts/{shift_id}/loss-accountings").json()
    assert [(x["revision"], x["rate_version"]) for x in listing] == [(1, 1), (2, 1)]
    # 过滤
    only_rev2 = client.get(
        f"/shifts/{shift_id}/loss-accountings", params={"revision": 2}
    ).json()
    assert [x["revision"] for x in only_rev2] == [2]


def test_accounting_errors_unknown_shift_unsettled_revision_and_rate(client):
    shift_id = _settled_shift_with_two_reasons(client)
    _publish(client, [_entry("failure", "00:00:00", "23:59:00", "1")])

    r = client.post("/shifts/9999/loss-accountings", json={"rate_version": 1})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "SHIFT_NOT_FOUND"

    r = client.post(
        f"/shifts/{shift_id}/loss-accountings", json={"rate_version": 99}
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RATE_VERSION_NOT_FOUND"

    r = client.post(
        f"/shifts/{shift_id}/loss-accountings",
        json={"rate_version": 1, "revision": 99},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "REVISION_NOT_FOUND"

    # rate_version=0 → 422（pydantic ge=1）
    r = client.post(
        f"/shifts/{shift_id}/loss-accountings", json={"rate_version": 0}
    )
    assert r.status_code == 422


def test_accounting_unsettled_shift_rejected(client):
    client.post("/devices", json=DEVICE)
    r = client.post(
        "/shifts",
        json={
            "device_id": "CNC-01",
            "start": "2026-10-01T08:00:00+08:00",
            "end": "2026-10-01T16:00:00+08:00",
        },
    )
    shift_id = r.json()["id"]
    _publish(client, [_entry("failure", "00:00:00", "23:59:00", "1")])
    resp = client.post(f"/shifts/{shift_id}/loss-accountings", json={"rate_version": 1})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RESULT_NOT_FOUND"


def test_accounting_legacy_revision_without_segments_rejected(client):
    shift_id = _settled_shift_with_two_reasons(client)
    _publish(client, [_entry("failure", "00:00:00", "23:59:00", "1")])
    with engine.begin() as conn:
        conn.execute(text("UPDATE shift_results SET segments_available = FALSE"))
    r = client.post(f"/shifts/{shift_id}/loss-accountings", json={"rate_version": 1})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "SEGMENTS_UNAVAILABLE"
    # 拒绝后不得残留账
    with engine.begin() as conn:
        n = conn.execute(text("SELECT count(*) FROM loss_accountings")).scalar()
    assert n == 0


def test_rate_version_device_scoping(client):
    # 两个设备各自版本号独立；A 的班次不能用 B 的费率版本
    shift_id = _settled_shift_with_two_reasons(client, "CNC-01")
    client.post("/devices", json={"id": "CNC-02", "name": "加工中心2号"})
    _publish(
        client, [_entry("failure", "00:00:00", "23:59:00", "1")], device="CNC-02"
    )
    r = client.post(
        f"/shifts/{shift_id}/loss-accountings", json={"rate_version": 1}
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RATE_VERSION_NOT_FOUND"


def test_concurrent_identical_accounting_creates_single_row(client):
    shift_id = _settled_shift_with_two_reasons(client)
    _publish(client, [_entry("failure", "00:00:00", "23:59:00", "1")])
    barrier = threading.Barrier(4)
    statuses: list[int] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait(timeout=10)
            r = client.post(
                f"/shifts/{shift_id}/loss-accountings", json={"rate_version": 1}
            )
            with lock:
                statuses.append(r.status_code)
                assert r.status_code in (200, 201), r.text
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(statuses).count(201) == 1
    assert sorted(statuses).count(200) == 3
    with engine.begin() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM loss_accountings "
                "WHERE shift_id=:s AND revision=1 AND rate_version=1"
            ),
            {"s": shift_id},
        ).scalar()
        npieces = conn.execute(
            text(
                "SELECT count(*) FROM loss_accounting_pieces p "
                "JOIN loss_accountings a ON a.id = p.accounting_id "
                "WHERE a.shift_id=:s"
            ),
            {"s": shift_id},
        ).scalar()
    assert n == 1
    # 2 个定价段 + 1 个待定价段，无重复明细
    assert npieces == 3
