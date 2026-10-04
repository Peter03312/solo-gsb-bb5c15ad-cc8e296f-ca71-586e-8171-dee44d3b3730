"""现场停机对账（POST /devices/{id}/downtime-reconciliation）端到端测试。

覆盖：现场裁剪合并、按固定序号选取各相交班次最后修订及固化明细、三类归因段
并集代表系统停机、仅已结算覆盖分钟内比较、覆盖外现场分钟计未对账、
相邻班次覆盖并集、seq 缺省固定最新序号/显式序号可重放/序号越界、
历史修订明细不可用拒绝、窗口与区间非法输入、未知设备。

运行方式：docker compose exec app pytest tests/test_downtime_reconciliation.py
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
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
PATH = "/devices/CNC-01/downtime-reconciliation"


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _shift(client, day: int, sh: int = 8, eh: int = 16, device="CNC-01") -> int:
    r = client.post(
        "/shifts",
        json={
            "device_id": device,
            "start": f"2026-10-{day:02d}T{sh:02d}:00:00+08:00",
            "end": f"2026-10-{day:02d}T{eh:02d}:00:00+08:00",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _event(client, source: str, version: int, day: int, sh, eh, reason: str) -> None:
    def _fmt(x):
        if isinstance(x, int):
            return f"{x:02d}:00:00"
        hh, mm = x
        return f"{hh:02d}:{mm:02d}:00"

    r = client.post(
        "/events",
        json={
            "device_id": "CNC-01",
            "source_id": source,
            "version": version,
            "start": f"2026-10-{day:02d}T{_fmt(sh)}+08:00",
            "end": f"2026-10-{day:02d}T{_fmt(eh)}+08:00",
            "reason": reason,
        },
    )
    assert r.status_code == 201, r.text


def _settle(client, shift_id) -> None:
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.status_code == 200, r.text


def _reconcile(client, body, device="CNC-01"):
    return client.post(f"/devices/{device}/downtime-reconciliation", json=body)


def _window(day0=1, day1=2):
    return {
        "start": f"2026-10-{day0:02d}T00:00:00+08:00",
        "end": f"2026-10-{day1:02d}T00:00:00+08:00",
    }


def _iv(day: int, sh: str, eh: str):
    return {
        "start": f"2026-10-{day:02d}T{sh}+08:00",
        "end": f"2026-10-{day:02d}T{eh}+08:00",
    }


# ---------------------------------------------------------------- 基本差异


def test_basic_field_vs_system_diff(client):
    client.post("/devices", json=DEVICE)
    shift_id = _shift(client, 1)
    # 系统：故障 10:00-12:00（120 分钟），结算 → seq=1
    _event(client, "PLC-9", 1, 1, 10, 12, "failure")
    _settle(client, shift_id)

    # 现场：09:30-10:30（与系统重叠 10:00-10:30）
    body = {**_window(), "field_intervals": [_iv(1, "09:30:00", "10:30:00")]}
    r = _reconcile(client, body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seq"] == 1
    assert data["settled_minutes"] == 480
    # 现场有系统无：09:30-10:00（30 分钟，落在已结算班次内）
    assert data["field_only"] == [
        {"start": "2026-10-01T01:30:00Z", "end": "2026-10-01T02:00:00Z", "minutes": 30}
    ]
    assert data["field_only_minutes"] == 30
    # 系统有现场无：10:30-12:00（90 分钟）
    assert data["system_only"] == [
        {"start": "2026-10-01T02:30:00Z", "end": "2026-10-01T04:00:00Z", "minutes": 90}
    ]
    assert data["system_only_minutes"] == 90
    assert data["unreconciled"] == []
    assert data["unreconciled_minutes"] == 0
    assert data["reconciled_shifts"] == [
        {"shift_id": shift_id, "revision": 1, "seq": 1}
    ]


def test_field_outside_settled_shift_is_unreconciled(client):
    client.post("/devices", json=DEVICE)
    shift_id = _shift(client, 1)
    _event(client, "PLC-9", 1, 1, 10, 11, "failure")
    _settle(client, shift_id)

    # 现场 12:00-13:00（在班次 08-16 内）与 20:00-21:00（班次外）
    body = {
        **_window(),
        "field_intervals": [
            _iv(1, "12:00:00", "13:00:00"),
            _iv(1, "20:00:00", "21:00:00"),
        ],
    }
    r = _reconcile(client, body)
    assert r.status_code == 200, r.text
    data = r.json()
    # 班次内现场多出 → 差异
    assert data["field_only"] == [
        {"start": "2026-10-01T04:00:00Z", "end": "2026-10-01T05:00:00Z", "minutes": 60}
    ]
    assert data["field_only_minutes"] == 60
    # 系统 10:00-11:00 现场无记录 → 差异
    assert data["system_only_minutes"] == 60
    # 班次外 → 未对账，不算差异
    assert data["unreconciled"] == [
        {"start": "2026-10-01T12:00:00Z", "end": "2026-10-01T13:00:00Z", "minutes": 60}
    ]
    assert data["unreconciled_minutes"] == 60


def test_field_intervals_clipped_merged_and_exact_match(client):
    client.post("/devices", json=DEVICE)
    shift_id = _shift(client, 1)
    _event(client, "PLC-9", 1, 1, 10, 12, "failure")
    _settle(client, shift_id)

    # 两段重叠且越窗，裁剪合并后恰好等于系统停机 10:00-12:00 → 无差异
    body = {
        **_window(),
        "field_intervals": [
            _iv(1, "09:00:00", "11:00:00"),
            _iv(1, "11:00:00", "13:00:00"),  # 端点相接也合并
        ],
    }
    r = _reconcile(client, body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["field_only"] == []
    assert data["system_only"] == []
    assert data["field_only_minutes"] == 0
    assert data["system_only_minutes"] == 0
    assert data["unreconciled_minutes"] == 0


# ---------------------------------------------------------------- 固定序号


def test_pinned_seq_uses_last_revision_at_seq_and_replay(client):
    client.post("/devices", json=DEVICE)
    shift_id = _shift(client, 1)
    _event(client, "PLC-9", 1, 1, 10, 12, "failure")
    _settle(client, shift_id)  # seq=1：系统 10:00-12:00
    # 迟到更正：故障缩短为 10:00-11:00 → seq=2：系统 10:00-11:00
    _event(client, "PLC-9", 2, 1, 10, 11, "failure")

    body = {**_window(), "field_intervals": [_iv(1, "10:00:00", "12:00:00")]}

    # 缺省 seq → 固定为当前最大序号 2：现场 10-12，系统 10-11 → field_only 11-12
    r = _reconcile(client, body)
    assert r.status_code == 200, r.text
    latest = r.json()
    assert latest["seq"] == 2
    assert latest["field_only"] == [
        {"start": "2026-10-01T03:00:00Z", "end": "2026-10-01T04:00:00Z", "minutes": 60}
    ]
    assert latest["system_only"] == []
    assert latest["reconciled_shifts"] == [
        {"shift_id": shift_id, "revision": 2, "seq": 2}
    ]

    # 显式 seq=1 → 取修订 1（系统 10-12），与现场完全一致，无差异
    r = _reconcile(client, {**body, "seq": 1})
    assert r.status_code == 200, r.text
    pinned = r.json()
    assert pinned["seq"] == 1
    assert pinned["field_only"] == []
    assert pinned["system_only"] == []
    assert pinned["reconciled_shifts"] == [
        {"shift_id": shift_id, "revision": 1, "seq": 1}
    ]

    # 再追加更正（seq=3）后，带 seq=1 重放结果不变
    _event(client, "PLC-9", 3, 1, (10, 30), 11, "failure")
    r = _reconcile(client, {**body, "seq": 1})
    assert r.status_code == 200, r.text
    replayed = r.json()
    assert replayed["field_only"] == pinned["field_only"]
    assert replayed["system_only"] == pinned["system_only"]
    assert replayed["reconciled_shifts"] == pinned["reconciled_shifts"]


def test_unsettled_shift_at_seq_not_in_coverage(client):
    client.post("/devices", json=DEVICE)
    s1 = _shift(client, 1)
    _event(client, "PLC-9", 1, 1, 10, 11, "failure")
    _settle(client, s1)  # seq=1
    # 第二个班次与窗口相交但未结算 → 不参与对账
    _shift(client, 2)

    body = {
        **_window(1, 3),
        "field_intervals": [
            _iv(1, "10:00:00", "11:00:00"),
            _iv(2, "10:00:00", "11:00:00"),  # 未结算班次日 → 未对账
        ],
    }
    r = _reconcile(client, body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert [s["shift_id"] for s in data["reconciled_shifts"]] == [s1]
    assert data["settled_minutes"] == 480  # 仅 10/1 的班次
    assert data["field_only"] == []
    assert data["system_only"] == []
    assert data["unreconciled"] == [
        {"start": "2026-10-02T02:00:00Z", "end": "2026-10-02T03:00:00Z", "minutes": 60}
    ]
    assert data["unreconciled_minutes"] == 60


def test_window_intersecting_partial_shift_and_two_shifts(client):
    client.post("/devices", json=DEVICE)
    s1 = _shift(client, 1)
    s2 = _shift(client, 2)
    _event(client, "A", 1, 1, 10, 12, "failure")
    _event(client, "B", 1, 2, 9, 10, "maintenance")
    _settle(client, s1)  # seq=1
    _settle(client, s2)  # seq=2

    # 窗口横跨两天班次：现场只报了 10/1 11:00-12:00
    body = {
        "start": "2026-10-01T11:00:00+08:00",
        "end": "2026-10-02T10:00:00+08:00",
        "field_intervals": [_iv(1, "11:00:00", "12:00:00")],
    }
    r = _reconcile(client, body)
    assert r.status_code == 200, r.text
    data = r.json()
    # 覆盖：10/1 11:00-16:00（300）+ 10/2 08:00-10:00（120）= 420
    assert data["settled_minutes"] == 420
    assert [s["shift_id"] for s in data["reconciled_shifts"]] == [s1, s2]
    # 系统差异：10/1 10:00-11:00 被窗口裁掉 → 现场覆盖段 11-12 与系统一致；
    # 10/2 09:00-10:00 保养 60 分钟现场无 → system_only
    assert data["system_only"] == [
        {"start": "2026-10-02T01:00:00Z", "end": "2026-10-02T02:00:00Z", "minutes": 60}
    ]
    assert data["system_only_minutes"] == 60
    assert data["field_only"] == []


def test_rulings_count_as_system_downtime_via_frozen_segments(client):
    client.post("/devices", json=DEVICE)
    shift_id = _shift(client, 1)
    _event(client, "PLC-9", 1, 1, 10, 11, "failure")
    _settle(client, shift_id)  # seq=1：系统 10:00-11:00
    # 现场裁定 14:00-15:00 为待判 → seq=2：系统新增 14:00-15:00 停机
    r = client.post(
        f"/shifts/{shift_id}/adjudications",
        json={
            "ruling_no": "R-1",
            **_iv(1, "14:00:00", "15:00:00"),
            "reason": "pending",
            "note": "现场待判",
        },
    )
    assert r.status_code == 201, r.text

    body = {**_window(), "field_intervals": [_iv(1, "10:00:00", "11:00:00")]}
    r = _reconcile(client, body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seq"] == 2
    # 裁定分钟属于系统停机：14:00-15:00 现场无 → system_only
    assert data["system_only"] == [
        {"start": "2026-10-01T06:00:00Z", "end": "2026-10-01T07:00:00Z", "minutes": 60}
    ]
    assert data["system_only_minutes"] == 60
    assert [s["revision"] for s in data["reconciled_shifts"]] == [2]


# ---------------------------------------------------------------- 错误


def test_seq_beyond_latest_rejected(client):
    client.post("/devices", json=DEVICE)
    shift_id = _shift(client, 1)
    _event(client, "PLC-9", 1, 1, 10, 11, "failure")
    _settle(client, shift_id)  # seq=1

    body = {**_window(), "field_intervals": [], "seq": 99}
    r = _reconcile(client, body)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_SEQ"


def test_seq_zero_empty_when_no_changes(client):
    client.post("/devices", json=DEVICE)
    _shift(client, 1)
    # 无任何结算：seq 缺省为 0；显式 seq=0 合法 → 空对账（无覆盖，现场全部未对账）
    body = {**_window(), "field_intervals": [_iv(1, "10:00:00", "11:00:00")]}
    r = _reconcile(client, body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seq"] == 0
    assert data["reconciled_shifts"] == []
    assert data["settled_minutes"] == 0
    assert data["field_only"] == []
    assert data["system_only"] == []
    assert data["unreconciled_minutes"] == 60

    r = _reconcile(client, {**body, "seq": 0})
    assert r.status_code == 200
    assert r.json()["seq"] == 0


def test_legacy_revision_without_segments_rejected(client):
    client.post("/devices", json=DEVICE)
    shift_id = _shift(client, 1)
    _event(client, "PLC-9", 1, 1, 10, 11, "failure")
    _settle(client, shift_id)  # 新修订恒有明细
    # 模拟结构引入前的历史修订：明细标记不可用
    with engine.begin() as conn:
        conn.execute(text("UPDATE shift_results SET segments_available = FALSE"))

    body = {**_window(), "field_intervals": []}
    r = _reconcile(client, body)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "SEGMENTS_UNAVAILABLE"


def test_unknown_device_404(client):
    body = {**_window(), "field_intervals": []}
    r = _reconcile(client, body, device="NOPE")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


@pytest.mark.parametrize(
    "overrides",
    [
        {"start": "2026-10-01T00:00:00"},  # 起点缺时区
        {"end": "2026-10-01T00:00:30+08:00"},  # 未对齐整分钟
        {"start": "2026-10-02T00:00:00+08:00"},  # start >= end
        {"seq": -1},  # 序号为负
        {"field_intervals": [{"start": "2026-10-01T11:00:00+08:00",
                              "end": "2026-10-01T10:00:00+08:00"}]},  # 区间倒置
        {"field_intervals": [{"start": "2026-10-01T10:00:30+08:00",
                              "end": "2026-10-01T11:00:00+08:00"}]},  # 秒非 00
    ],
)
def test_invalid_inputs_rejected(client, overrides):
    client.post("/devices", json=DEVICE)
    body = {**_window(), "field_intervals": []}
    body.update(overrides)
    r = _reconcile(client, body)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_empty_field_intervals_defaults_ok(client):
    client.post("/devices", json=DEVICE)
    shift_id = _shift(client, 1)
    _event(client, "PLC-9", 1, 1, 10, 11, "failure")
    _settle(client, shift_id)

    r = _reconcile(client, {**_window()})  # 不传 field_intervals
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["field_only"] == []
    assert data["system_only_minutes"] == 60  # 现场无记录：系统停机全部为差异
