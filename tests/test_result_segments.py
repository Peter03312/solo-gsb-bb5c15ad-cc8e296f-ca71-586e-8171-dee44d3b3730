"""归因区间明细（GET /shifts/{id}/result/segments）端到端测试：需要可用的 PostgreSQL。

覆盖：结算/更正/裁定/撤销各触发器下明细与修订同事务固化、按起点排序互不重叠、
依据形态（生效裁定单号 / 事件来源与版本列表）、未停机分钟不返回、各原因段分钟
之和等于修订总数、历史修订不可变、未固化明细的历史修订明确返回不可用、
未知班次/修订沿用现有错误语义。

运行方式：docker compose exec app pytest tests/test_result_segments.py
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "shift_result_segments",
    "manual_adjudications",
    "shift_results",
    "event_versions",
    "event_sources",
    "shifts",
    "devices",
    "change_counter",
)

DEVICE = {"id": "CNC-01", "name": "加工中心1号"}
SHIFT = {  # 早班 08:00-16:00 (+08:00) = 00:00-08:00 (UTC)
    "device_id": "CNC-01",
    "start": "2026-10-01T08:00:00+08:00",
    "end": "2026-10-01T16:00:00+08:00",
}
E_MAINT = {  # 保养 10:00-11:00
    "device_id": "CNC-01",
    "source_id": "MES-1",
    "version": 1,
    "start": "2026-10-01T10:00:00+08:00",
    "end": "2026-10-01T11:00:00+08:00",
    "reason": "maintenance",
}
E_FAIL = {  # 故障 10:30-12:00
    "device_id": "CNC-01",
    "source_id": "PLC-9",
    "version": 1,
    "start": "2026-10-01T10:30:00+08:00",
    "end": "2026-10-01T12:00:00+08:00",
    "reason": "failure",
}

UTC = timezone.utc
# 班次窗口（UTC）：2026-10-01 00:00 - 08:00
SHIFT_UTC0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def u(minutes: int) -> datetime:
    """班次 UTC 起点 + 分钟数。10:00(+08:00) = 02:00(UTC) = 120 分钟。"""
    return SHIFT_UTC0 + timedelta(minutes=minutes)


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(UTC)


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _settled_shift(client) -> int:
    """班次 + 保养 [10:00,11:00) + 故障 [10:30,12:00)，结算后修订 1：m60 f60。"""
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = client.post("/shifts", json=SHIFT)
    assert r.status_code == 201
    sid = r.json()["id"]
    assert client.post("/events", json=E_MAINT).status_code == 201
    assert client.post("/events", json=E_FAIL).status_code == 201
    assert client.post(f"/shifts/{sid}/settle").status_code == 200
    return sid


def _get_segments(client, sid: int, revision: int | None = None) -> dict:
    params = {} if revision is None else {"revision": revision}
    r = client.get(f"/shifts/{sid}/result/segments", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _shape(segments) -> list:
    """把响应段列表规整为便于断言的 (start, end, reason, basis) 元组列表。"""
    out = []
    for s in segments:
        basis = s["basis"]
        if basis["type"] == "ruling":
            b = ("ruling", basis["ruling_no"])
        else:
            b = (
                "events",
                tuple(
                    (ev["source_id"], ev["version"], ev["reason"])
                    for ev in basis["event_versions"]
                ),
            )
        out.append((parse(s["start"]), parse(s["end"]), s["reason"], b, s["minutes"]))
    return out


def _assert_sums_match_revision(client, sid: int, revision: int, body: dict):
    """各原因段分钟之和须等于该修订总数；段列表按起点排序且互不重叠。"""
    result = client.get(f"/shifts/{sid}/result", params={"revision": revision}).json()
    sums = {"maintenance": 0, "failure": 0, "pending": 0}
    last_end = None
    for s in body["segments"]:
        start, end = parse(s["start"]), parse(s["end"])
        assert start < end
        if last_end is not None:
            assert last_end <= start  # 互不重叠、按起点排序
        last_end = end
        assert s["minutes"] == int((end - start).total_seconds()) // 60
        sums[s["reason"]] += s["minutes"]
    assert sums == result["totals"]


# ---------------------------------------------------------------- 结算与更正


def test_settle_segments_explain_totals(client):
    sid = _settled_shift(client)
    body = _get_segments(client, sid)
    assert body["shift_id"] == sid
    assert body["revision"] == 1
    assert body["latest_revision"] == 1
    assert body["available"] is True
    # 保养 10:00-11:00（UTC 02:00-03:00）；故障被保养覆盖 10:30-11:00，计 11:00-12:00
    assert _shape(body["segments"]) == [
        (u(120), u(180), "maintenance", ("events", (("MES-1", 1, "maintenance"),)), 60),
        (u(180), u(240), "failure", ("events", (("PLC-9", 1, "failure"),)), 60),
    ]
    _assert_sums_match_revision(client, sid, 1, body)


def test_correction_writes_new_segments_and_keeps_old(client):
    sid = _settled_shift(client)
    # 迟到更正：故障缩短为 10:30-11:30（v2）→ 修订 2：m60 f30
    r = client.post(
        "/events",
        json={**E_FAIL, "version": 2, "end": "2026-10-01T11:30:00+08:00"},
    )
    assert r.status_code == 201
    assert r.json()["recomputed"][0]["revision"] == 2

    # 缺省查询 = 最新修订（修订 2）的明细
    body = _get_segments(client, sid)
    assert body["revision"] == 2
    assert body["latest_revision"] == 2
    assert _shape(body["segments"]) == [
        (u(120), u(180), "maintenance", ("events", (("MES-1", 1, "maintenance"),)), 60),
        (u(180), u(210), "failure", ("events", (("PLC-9", 2, "failure"),)), 30),
    ]
    _assert_sums_match_revision(client, sid, 2, body)

    # 旧修订明细保持原样（仍引用当时的 PLC-9 v1）
    body1 = _get_segments(client, sid, revision=1)
    assert body1["revision"] == 1
    assert body1["latest_revision"] == 2
    assert _shape(body1["segments"]) == [
        (u(120), u(180), "maintenance", ("events", (("MES-1", 1, "maintenance"),)), 60),
        (u(180), u(240), "failure", ("events", (("PLC-9", 1, "failure"),)), 60),
    ]


def test_correction_without_change_creates_no_revision_but_old_segments_stay(client):
    sid = _settled_shift(client)
    # 同内容递增版本：分钟数不变 → 无新修订；修订 1 明细仍可查
    r = client.post("/events", json={**E_MAINT, "version": 2})
    assert r.status_code == 201
    assert r.json()["recomputed"] == []
    body = _get_segments(client, sid)
    assert body["revision"] == 1
    assert body["available"] is True
    assert len(body["segments"]) == 2


def test_zero_downtime_shift_has_empty_segment_list(client):
    assert client.post("/devices", json=DEVICE).status_code == 201
    sid = client.post("/shifts", json=SHIFT).json()["id"]
    client.post(f"/shifts/{sid}/settle")
    body = _get_segments(client, sid)
    # 无停机分钟：明细可用但为空列表（与"未固化"明确区分）
    assert body["available"] is True
    assert body["segments"] == []


# ---------------------------------------------------------------- 裁定 / 撤销


def test_adjudication_segments_use_ruling_basis(client):
    sid = _settled_shift(client)
    # 故障更正为 10:30-11:30（v2）→ 修订 2：m60 f30
    client.post("/events", json={**E_FAIL, "version": 2, "end": "2026-10-01T11:30:00+08:00"})
    # 裁定 R-001：10:30-11:00 改判待判 → 修订 3：m30 f30 p30
    r = client.post(
        f"/shifts/{sid}/adjudications",
        json={
            "ruling_no": "R-001",
            "start": "2026-10-01T10:30:00+08:00",
            "end": "2026-10-01T11:00:00+08:00",
            "reason": "pending",
            "note": "传感器无记录，待质检复核",
        },
    )
    assert r.status_code == 201
    assert r.json()["revision"] == 3

    body = _get_segments(client, sid)
    assert body["revision"] == 3
    assert _shape(body["segments"]) == [
        (u(120), u(150), "maintenance", ("events", (("MES-1", 1, "maintenance"),)), 30),
        (u(150), u(180), "pending", ("ruling", "R-001"), 30),
        (u(180), u(210), "failure", ("events", (("PLC-9", 2, "failure"),)), 30),
    ]
    _assert_sums_match_revision(client, sid, 3, body)

    # 更晚生效的重叠裁定 R-002：10:45-11:15 改判故障 → 修订 4
    r = client.post(
        f"/shifts/{sid}/adjudications",
        json={
            "ruling_no": "R-002",
            "start": "2026-10-01T10:45:00+08:00",
            "end": "2026-10-01T11:15:00+08:00",
            "reason": "failure",
            "note": "现场确认为传动故障",
        },
    )
    assert r.json()["revision"] == 4
    body = _get_segments(client, sid)
    assert _shape(body["segments"]) == [
        (u(120), u(150), "maintenance", ("events", (("MES-1", 1, "maintenance"),)), 30),
        (u(150), u(165), "pending", ("ruling", "R-001"), 15),
        (u(165), u(195), "failure", ("ruling", "R-002"), 30),
        (u(195), u(210), "failure", ("events", (("PLC-9", 2, "failure"),)), 15),
    ]
    _assert_sums_match_revision(client, sid, 4, body)

    # 撤销 R-002 → 修订 5：重叠分钟回退到 R-001，明细与修订 3 一致
    r = client.post(f"/shifts/{sid}/adjudications/R-002/revoke")
    assert r.json()["revision"] == 5
    body = _get_segments(client, sid)
    assert _shape(body["segments"]) == [
        (u(120), u(150), "maintenance", ("events", (("MES-1", 1, "maintenance"),)), 30),
        (u(150), u(180), "pending", ("ruling", "R-001"), 30),
        (u(180), u(210), "failure", ("events", (("PLC-9", 2, "failure"),)), 30),
    ]
    _assert_sums_match_revision(client, sid, 5, body)

    # 裁定撤销后，修订 3 / 4 的明细仍保持原样
    assert _shape(_get_segments(client, sid, revision=3)["segments"]) == [
        (u(120), u(150), "maintenance", ("events", (("MES-1", 1, "maintenance"),)), 30),
        (u(150), u(180), "pending", ("ruling", "R-001"), 30),
        (u(180), u(210), "failure", ("events", (("PLC-9", 2, "failure"),)), 30),
    ]
    assert _shape(_get_segments(client, sid, revision=4)["segments"])[2] == (
        u(165),
        u(195),
        "failure",
        ("ruling", "R-002"),
        30,
    )


def test_ruling_without_minute_change_keeps_latest_revision_segments(client):
    sid = _settled_shift(client)
    # 11:00-12:00 本就是故障：裁定同原因 → 无新修订，最新明细不变
    r = client.post(
        f"/shifts/{sid}/adjudications",
        json={
            "ruling_no": "R-NEUTRAL",
            "start": "2026-10-01T11:00:00+08:00",
            "end": "2026-10-01T12:00:00+08:00",
            "reason": "failure",
            "note": "与事件一致",
        },
    )
    assert r.status_code == 201
    assert r.json()["revision"] is None
    body = _get_segments(client, sid)
    assert body["revision"] == 1
    assert _shape(body["segments"])[1] == (
        u(180),
        u(240),
        "failure",
        ("events", (("PLC-9", 1, "failure"),)),
        60,
    )


# ---------------------------------------------------------------- 批量补传与预演确认


def test_batch_and_confirm_also_persist_segments(client):
    sid = _settled_shift(client)
    # 批量补传：保养延长到 12:00（MES-1 v2）→ 修订 2：m120 f0
    r = client.post(
        "/events/batch",
        json={
            "device_id": "CNC-01",
            "events": [
                {
                    "source_id": "MES-1",
                    "version": 2,
                    "start": "2026-10-01T10:00:00+08:00",
                    "end": "2026-10-01T12:00:00+08:00",
                    "reason": "maintenance",
                }
            ],
        },
    )
    assert r.status_code == 201
    assert r.json()["recomputed"][0]["revision"] == 2
    body = _get_segments(client, sid)
    assert _shape(body["segments"]) == [
        (u(120), u(240), "maintenance", ("events", (("MES-1", 2, "maintenance"),)), 120),
    ]
    _assert_sums_match_revision(client, sid, 2, body)

    # 预演-确认：故障改到 13:00-14:00（PLC-9 v2）→ 修订 3：m120 f60
    r = client.post(
        "/events/preview",
        json={
            **E_FAIL,
            "version": 2,
            "start": "2026-10-01T13:00:00+08:00",
            "end": "2026-10-01T14:00:00+08:00",
        },
    )
    assert r.status_code == 200
    token = r.json()["confirm_token"]
    r = client.post("/events/confirm", json={"confirm_token": token})
    assert r.status_code == 201
    assert r.json()["recomputed"][0]["revision"] == 3
    body = _get_segments(client, sid)
    assert _shape(body["segments"]) == [
        (u(120), u(240), "maintenance", ("events", (("MES-1", 2, "maintenance"),)), 120),
        (u(300), u(360), "failure", ("events", (("PLC-9", 2, "failure"),)), 60),
    ]
    _assert_sums_match_revision(client, sid, 3, body)


# ---------------------------------------------------------------- 历史修订不可用


def test_legacy_revision_reports_segments_unavailable(client):
    sid = _settled_shift(client)
    # 模拟本结构引入前生成的历史修订：无明细行且未固化标记
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM shift_result_segments"))
        conn.execute(
            text("UPDATE shift_results SET segments_available = FALSE")
        )
    body = _get_segments(client, sid, revision=1)
    assert body["available"] is False
    assert body["segments"] is None
    assert body["revision"] == 1
    # 缺省（最新修订）同样明确返回不可用，不以当前状态推算
    body = _get_segments(client, sid)
    assert body["available"] is False
    assert body["segments"] is None
    # 结果与修订查询不受影响
    assert client.get(f"/shifts/{sid}/result").json()["totals"] == {
        "maintenance": 60,
        "failure": 60,
        "pending": 0,
    }


def test_new_revision_after_legacy_one_has_segments(client):
    sid = _settled_shift(client)
    with engine.begin() as conn:  # 修订 1 变为"历史修订"
        conn.execute(text("DELETE FROM shift_result_segments"))
        conn.execute(text("UPDATE shift_results SET segments_available = FALSE"))
    # 新迟到更正产生修订 2：新修订正常固化明细，旧修订仍不可用
    client.post("/events", json={**E_FAIL, "version": 2, "end": "2026-10-01T11:30:00+08:00"})
    body = _get_segments(client, sid)
    assert body["revision"] == 2
    assert body["available"] is True
    assert _shape(body["segments"]) == [
        (u(120), u(180), "maintenance", ("events", (("MES-1", 1, "maintenance"),)), 60),
        (u(180), u(210), "failure", ("events", (("PLC-9", 2, "failure"),)), 30),
    ]
    body1 = _get_segments(client, sid, revision=1)
    assert body1["available"] is False
    assert body1["segments"] is None


# ---------------------------------------------------------------- 错误语义


def test_segments_error_semantics(client):
    # 未知班次 → 404 SHIFT_NOT_FOUND
    r = client.get("/shifts/99999/result/segments")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SHIFT_NOT_FOUND"

    # 未结算 → 404 RESULT_NOT_FOUND
    assert client.post("/devices", json=DEVICE).status_code == 201
    sid = client.post("/shifts", json=SHIFT).json()["id"]
    r = client.get(f"/shifts/{sid}/result/segments")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RESULT_NOT_FOUND"

    # 未知修订 → 404 REVISION_NOT_FOUND
    client.post(f"/shifts/{sid}/settle")
    r = client.get(f"/shifts/{sid}/result/segments", params={"revision": 99})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "REVISION_NOT_FOUND"

    # 非法修订号 → 422
    r = client.get(f"/shifts/{sid}/result/segments", params={"revision": 0})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"
