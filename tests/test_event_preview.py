"""事件预演与确认（POST /events/preview、POST /events/confirm）端到端测试：
需要可用的 PostgreSQL（在 Compose 环境内运行）。

运行方式：docker compose exec app pytest tests/test_event_preview.py
"""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "manual_adjudications",
    "shift_results",
    "event_versions",
    "event_sources",
    "shifts",
    "devices",
    "change_counter",
)

DEVICE = {"id": "CNC-01", "name": "加工中心1号"}
SHIFT = {  # 早班 08:00-16:00
    "device_id": "CNC-01",
    "start": "2026-10-01T08:00:00+08:00",
    "end": "2026-10-01T16:00:00+08:00",
}
SHIFT_B = {  # 晚班 16:00-24:00（端点相接，不重叠）
    "device_id": "CNC-01",
    "start": "2026-10-01T16:00:00+08:00",
    "end": "2026-10-02T00:00:00+08:00",
}


def ev(source, version, start, end, reason):
    return {
        "device_id": "CNC-01",
        "source_id": source,
        "version": version,
        "start": start,
        "end": end,
        "reason": reason,
    }


# 保养 10:00-11:00（MES-1 v1）；故障 10:30-12:00（PLC-9 v1）
E_MAINT_V1 = ev("MES-1", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "maintenance")
E_FAIL_V1 = ev("PLC-9", 1, "2026-10-01T10:30:00+08:00", "2026-10-01T12:00:00+08:00", "failure")
# 候选：故障缩短为 10:30-11:30（PLC-9 v2）
E_FAIL_V2 = ev("PLC-9", 2, "2026-10-01T10:30:00+08:00", "2026-10-01T11:30:00+08:00", "failure")


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _make_shift(client, shift=SHIFT) -> int:
    assert client.post("/devices", json=DEVICE).status_code == 201
    r = client.post("/shifts", json=shift)
    assert r.status_code == 201
    return r.json()["id"]


def _settled_shift(client) -> int:
    """班次 + 保养/故障各一条 + 结算：当前 totals = 保养 60 / 故障 60 / 待判 0。"""
    shift_id = _make_shift(client)
    assert client.post("/events", json=E_MAINT_V1).status_code == 201
    assert client.post("/events", json=E_FAIL_V1).status_code == 201
    assert client.post(f"/shifts/{shift_id}/settle").status_code == 200
    return shift_id


def _preview(client, payload):
    return client.post("/events/preview", json=payload)


def _confirm(client, token):
    return client.post("/events/confirm", json={"confirm_token": token})


def _changes(client):
    return client.get("/changes", params={"cursor": 0, "limit": 500}).json()


def _result(client, shift_id):
    return client.get(f"/shifts/{shift_id}/result").json()


# ---------------------------------------------------------------- 预演（只读）


def test_preview_basic_impacts(client):
    shift_id = _settled_shift(client)
    r = _preview(client, E_FAIL_V2)
    assert r.status_code == 200
    body = r.json()
    assert body["device_id"] == "CNC-01"
    assert body["source_id"] == "PLC-9"
    assert body["version"] == 2
    assert body["idempotent"] is False
    assert body["confirm_token"]
    assert body["impacts"] == [
        {
            "shift_id": shift_id,
            "revision": 1,
            "current": {"maintenance": 60, "failure": 60, "pending": 0},
            "proposed": {"maintenance": 60, "failure": 30, "pending": 0},
            "delta": {"maintenance": 0, "failure": -30, "pending": 0},
            "changed": True,
        }
    ]


def test_preview_is_read_only(client):
    shift_id = _settled_shift(client)
    before = _result(client, shift_id)
    changes_before = _changes(client)["changes"]

    r = _preview(client, E_FAIL_V2)
    assert r.status_code == 200
    token = r.json()["confirm_token"]

    # 预演不写入事件、修订或变更：结果、修订与变更流保持原样
    assert _result(client, shift_id) == before
    assert len(client.get(f"/shifts/{shift_id}/revisions").json()) == 1
    assert _changes(client)["changes"] == changes_before

    # 版本链未推进：候选版本再次预演仍是"新版本"（非幂等），直接上报仍是新建
    r2 = _preview(client, E_FAIL_V2)
    assert r2.status_code == 200 and r2.json()["idempotent"] is False
    r3 = client.post("/events", json=E_FAIL_V2)
    assert r3.status_code == 201 and r3.json()["idempotent"] is False

    # 直接上报推进版本链后，旧凭据确认 → 幂等返回（同版本同内容已存在）
    r4 = _confirm(client, token)
    assert r4.status_code == 200 and r4.json()["idempotent"] is True


def test_preview_lists_unchanged_shifts(client):
    """结果不变的班次也列出：候选版本与原版本同内容 → changed=false。"""
    shift_id = _settled_shift(client)
    same_content_v2 = ev("MES-1", 2, "2026-10-01T10:00:00+08:00",
                         "2026-10-01T11:00:00+08:00", "maintenance")
    r = _preview(client, same_content_v2)
    assert r.status_code == 200
    impacts = r.json()["impacts"]
    assert len(impacts) == 1
    assert impacts[0]["shift_id"] == shift_id
    assert impacts[0]["changed"] is False
    assert impacts[0]["current"] == impacts[0]["proposed"]
    assert impacts[0]["delta"] == {"maintenance": 0, "failure": 0, "pending": 0}

    # 确认提交：201 落库事件，但分钟数未变 → 不产生修订与变更
    r2 = _confirm(client, r.json()["confirm_token"])
    assert r2.status_code == 201
    assert r2.json()["idempotent"] is False
    assert r2.json()["recomputed"] == []
    assert _result(client, shift_id)["revision"] == 1
    assert len(_changes(client)["changes"]) == 1  # 仍只有结算变更
    # 版本链已推进：v2 再次预演为幂等
    assert _preview(client, same_content_v2).json()["idempotent"] is True


def test_preview_union_of_old_and_candidate_intervals(client):
    """相关班次按旧版 ∪ 候选版区间并集确定：候选移出原区间后，两端的班次都列出。"""
    assert client.post("/devices", json=DEVICE).status_code == 201
    s1 = client.post("/shifts", json=SHIFT).json()["id"]  # 班次 1：08-16
    s2 = client.post("/shifts", json=SHIFT_B).json()["id"]  # 班次 2：16-24
    # MES-1 v1 保养 15:00-17:00：跨两个班次
    assert client.post("/events", json=ev(
        "MES-1", 1, "2026-10-01T15:00:00+08:00", "2026-10-01T17:00:00+08:00",
        "maintenance")).status_code == 201
    assert client.post(f"/shifts/{s1}/settle").status_code == 200
    assert client.post(f"/shifts/{s2}/settle").status_code == 200

    # 候选 v2 移到 10:00-11:00（只在班次 1）：并集仍覆盖两个班次
    r = _preview(client, ev("MES-1", 2, "2026-10-01T10:00:00+08:00",
                            "2026-10-01T11:00:00+08:00", "maintenance"))
    assert r.status_code == 200
    impacts = {i["shift_id"]: i for i in r.json()["impacts"]}
    assert set(impacts) == {s1, s2}
    assert impacts[s1]["proposed"] == {"maintenance": 60, "failure": 0, "pending": 0}
    assert impacts[s1]["delta"] == {"maintenance": 0, "failure": 0, "pending": 0}
    assert impacts[s1]["changed"] is False  # 班次 1 原本就有 15:00-16:00 的 60 分钟保养
    assert impacts[s2]["current"] == {"maintenance": 60, "failure": 0, "pending": 0}
    assert impacts[s2]["proposed"] == {"maintenance": 0, "failure": 0, "pending": 0}
    assert impacts[s2]["changed"] is True


def test_preview_respects_active_adjudications(client):
    """拟变分钟数按当前生效裁定计算：裁定分钟不被事件改动。"""
    shift_id = _settled_shift(client)
    # 裁定 10:30-11:00 为待判（覆盖事件归因）→ 当前 m=30, f=60, p=30
    r = client.post(f"/shifts/{shift_id}/adjudications", json={
        "ruling_no": "R-1", "start": "2026-10-01T10:30:00+08:00",
        "end": "2026-10-01T11:00:00+08:00", "reason": "pending", "note": "待复核",
    })
    assert r.status_code == 201 and r.json()["revision"] == 2

    r = _preview(client, E_FAIL_V2)  # 故障缩短为 10:30-11:30
    impact = r.json()["impacts"][0]
    assert impact["current"] == {"maintenance": 30, "failure": 60, "pending": 30}
    # 拟变：裁定分钟（10:30-11:00 待判）不变；故障只剩 11:00-11:30 的 30 分钟
    assert impact["proposed"] == {"maintenance": 30, "failure": 30, "pending": 30}
    assert impact["delta"] == {"maintenance": 0, "failure": -30, "pending": 0}
    assert impact["changed"] is True


def test_preview_idempotent_existing_version(client):
    _settled_shift(client)
    r = _preview(client, E_MAINT_V1)  # 同来源同版本同内容
    assert r.status_code == 200
    body = r.json()
    assert body["idempotent"] is True
    assert body["impacts"] == []

    # 幂等预演的确认 → 现有幂等语义：200，无任何写入
    r2 = _confirm(client, body["confirm_token"])
    assert r2.status_code == 200
    assert r2.json()["idempotent"] is True
    assert r2.json()["recomputed"] == []
    assert len(_changes(client)["changes"]) == 1


def test_preview_version_conflict_and_gap(client):
    _settled_shift(client)
    conflict = ev("MES-1", 1, "2026-10-01T10:00:00+08:00",
                  "2026-10-01T11:30:00+08:00", "maintenance")
    r = _preview(client, conflict)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_CONFLICT"

    gap = ev("MES-1", 3, "2026-10-01T10:00:00+08:00",
             "2026-10-01T11:00:00+08:00", "maintenance")
    r = _preview(client, gap)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_GAP"


def test_preview_unknown_device_and_invalid_input(client):
    _settled_shift(client)
    r = _preview(client, ev("MES-1", 2, "2026-10-01T10:00:00+08:00",
                            "2026-10-01T11:00:00+08:00", "maintenance")
                 | {"device_id": "NOPE"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"

    bad_sec = ev("MES-1", 2, "2026-10-01T10:00:30+08:00",
                 "2026-10-01T11:00:00+08:00", "maintenance")
    r = _preview(client, bad_sec)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"

    no_tz = ev("MES-1", 2, "2026-10-01T10:00:00",
               "2026-10-01T11:00:00", "maintenance")
    assert _preview(client, no_tz).status_code == 422

    bad_reason = ev("MES-1", 2, "2026-10-01T10:00:00+08:00",
                    "2026-10-01T11:00:00+08:00", "oops")
    assert _preview(client, bad_reason).status_code == 422

    bad_range = ev("MES-1", 2, "2026-10-01T11:00:00+08:00",
                   "2026-10-01T10:00:00+08:00", "maintenance")
    assert _preview(client, bad_range).status_code == 422


def test_preview_new_source_does_not_write(client):
    """新来源的预演同样只读：不补建版本链、不落事件；确认可正常提交。"""
    shift_id = _settled_shift(client)
    cand = ev("NEW-1", 1, "2026-10-01T12:00:00+08:00",
              "2026-10-01T13:00:00+08:00", "pending")
    r = _preview(client, cand)
    assert r.status_code == 200 and r.json()["idempotent"] is False
    impact = r.json()["impacts"][0]
    assert impact["current"] == {"maintenance": 60, "failure": 60, "pending": 0}
    assert impact["proposed"] == {"maintenance": 60, "failure": 60, "pending": 60}

    # 再次预演仍是新版本（若第一次预演落了库，这里会变幂等）
    r2 = _preview(client, cand)
    assert r2.status_code == 200 and r2.json()["idempotent"] is False

    r3 = _confirm(client, r.json()["confirm_token"])
    assert r3.status_code == 201
    assert r3.json()["recomputed"] == [
        {"shift_id": shift_id, "revision": 2,
         "totals": {"maintenance": 60, "failure": 60, "pending": 60},
         "delta": {"maintenance": 0, "failure": 0, "pending": 60}}
    ]


# ---------------------------------------------------------------- 确认提交


def test_confirm_commits_and_matches_preview(client):
    shift_id = _settled_shift(client)
    token = _preview(client, E_FAIL_V2).json()["confirm_token"]

    r = _confirm(client, token)
    assert r.status_code == 201
    body = r.json()
    assert body["idempotent"] is False
    assert body["recomputed"] == [
        {"shift_id": shift_id, "revision": 2,
         "totals": {"maintenance": 60, "failure": 30, "pending": 0},
         "delta": {"maintenance": 0, "failure": -30, "pending": 0}}
    ]

    result = _result(client, shift_id)
    assert result["revision"] == 2
    assert result["totals"] == {"maintenance": 60, "failure": 30, "pending": 0}
    changes = _changes(client)["changes"]
    assert len(changes) == 2
    assert changes[-1]["trigger"] == "correction"
    assert changes[-1]["revision"] == 2

    # 事件已落库：同内容再预演为幂等
    assert _preview(client, E_FAIL_V2).json()["idempotent"] is True


def test_confirm_retry_is_idempotent(client):
    shift_id = _settled_shift(client)
    token = _preview(client, E_FAIL_V2).json()["confirm_token"]
    assert _confirm(client, token).status_code == 201

    r = _confirm(client, token)  # 相同确认重试
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    assert r.json()["recomputed"] == []
    assert _result(client, shift_id)["revision"] == 2  # 无新修订
    assert len(_changes(client)["changes"]) == 2  # 无新变更


def test_confirm_stale_after_adjudication(client):
    shift_id = _settled_shift(client)
    token = _preview(client, E_FAIL_V2).json()["confirm_token"]
    # 预演后班次新增裁定 → 裁定状态已变
    r = client.post(f"/shifts/{shift_id}/adjudications", json={
        "ruling_no": "R-1", "start": "2026-10-01T10:30:00+08:00",
        "end": "2026-10-01T11:00:00+08:00", "reason": "pending", "note": "",
    })
    assert r.status_code == 201

    r = _confirm(client, token)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PREVIEW_STALE"
    # 不得部分写入：版本链未推进、事件未落库（同内容直接上报仍是新建）
    r2 = client.post("/events", json=E_FAIL_V2)
    assert r2.status_code == 201 and r2.json()["idempotent"] is False


def test_confirm_stale_after_other_source_correction(client):
    _settled_shift(client)
    token = _preview(client, E_FAIL_V2).json()["confirm_token"]
    # 预演后另一来源新事件改变了班次结果
    r = client.post("/events", json=ev(
        "MES-2", 1, "2026-10-01T12:00:00+08:00", "2026-10-01T13:00:00+08:00",
        "failure"))
    assert r.status_code == 201 and r.json()["recomputed"] != []

    r = _confirm(client, token)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PREVIEW_STALE"


def test_confirm_stale_even_without_minute_change(client):
    """影响结果的事件状态已变即冲突，即使分钟数未变（无新修订）。"""
    shift_id = _settled_shift(client)
    token = _preview(client, E_FAIL_V2).json()["confirm_token"]
    # 新事件 10:00-11:00 待判：完全被保养覆盖，分钟数不变、不产生修订
    r = client.post("/events", json=ev(
        "MES-2", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00",
        "pending"))
    assert r.status_code == 201 and r.json()["recomputed"] == []
    assert _result(client, shift_id)["revision"] == 1

    r = _confirm(client, token)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PREVIEW_STALE"


def test_confirm_stale_after_new_shift_settled(client):
    """预演后新结算了并集区间内的班次 → 相关班次集合已变。"""
    assert client.post("/devices", json=DEVICE).status_code == 201
    s1 = client.post("/shifts", json=SHIFT).json()["id"]
    s2 = client.post("/shifts", json=SHIFT_B).json()["id"]
    assert client.post("/events", json=ev(
        "MES-1", 1, "2026-10-01T15:00:00+08:00", "2026-10-01T17:00:00+08:00",
        "maintenance")).status_code == 201
    assert client.post(f"/shifts/{s1}/settle").status_code == 200
    # 班次 2 尚未结算：预演只列出班次 1
    cand = ev("MES-1", 2, "2026-10-01T15:00:00+08:00",
              "2026-10-01T18:00:00+08:00", "maintenance")
    r = _preview(client, cand)
    assert [i["shift_id"] for i in r.json()["impacts"]] == [s1]
    token = r.json()["confirm_token"]

    assert client.post(f"/shifts/{s2}/settle").status_code == 200
    r = _confirm(client, token)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PREVIEW_STALE"

    # 重新预演后确认即可提交：班次 2 被重算（班次 1 分钟数不变，无修订）
    token2 = _preview(client, cand).json()["confirm_token"]
    r = _confirm(client, token2)
    assert r.status_code == 201
    assert {x["shift_id"] for x in r.json()["recomputed"]} == {s2}


def test_confirm_version_conflict_after_other_writer(client):
    """预演后同来源同版本被写入异内容 → 沿用 VERSION_CONFLICT。"""
    _settled_shift(client)
    token = _preview(client, E_FAIL_V2).json()["confirm_token"]
    other = ev("PLC-9", 2, "2026-10-01T10:30:00+08:00",
               "2026-10-01T12:30:00+08:00", "failure")  # 同版本异内容
    assert client.post("/events", json=other).status_code == 201

    r = _confirm(client, token)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VERSION_CONFLICT"


def test_confirm_idempotent_after_same_content_written(client):
    """预演后同版本同内容已被他人上报 → 确认按现有幂等语义返回。"""
    _settled_shift(client)
    token = _preview(client, E_FAIL_V2).json()["confirm_token"]
    assert client.post("/events", json=E_FAIL_V2).status_code == 201

    r = _confirm(client, token)
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    assert r.json()["recomputed"] == []


def test_confirm_invalid_token(client):
    _settled_shift(client)
    for bad in ("", "abc", "x.y", "!!!"):
        r = _confirm(client, bad)
        assert r.status_code == 422, bad
        assert r.json()["detail"]["code"] in ("VALIDATION_ERROR", "INVALID_CONFIRM_TOKEN")

    r = _confirm(client, "aaaa.bbbb")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_CONFIRM_TOKEN"

    # 篡改合法凭据（改动载荷末位）→ 签名不符
    token = _preview(client, E_FAIL_V2).json()["confirm_token"]
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    r = _confirm(client, tampered)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_CONFIRM_TOKEN"


def test_confirm_rejects_snapshot_cursor(client):
    """快照续页游标与确认凭据不通用（类型不符即非法凭据）。"""
    assert client.post("/devices", json=DEVICE).status_code == 201
    s1 = client.post("/shifts", json=SHIFT).json()["id"]
    s2 = client.post("/shifts", json=SHIFT_B).json()["id"]
    assert client.post(f"/shifts/{s1}/settle").status_code == 200
    assert client.post(f"/shifts/{s2}/settle").status_code == 200
    r = client.get("/devices/CNC-01/shift-snapshots", params={
        "start": "2026-10-01T00:00:00+08:00", "end": "2026-10-04T00:00:00+08:00",
        "limit": 1,
    })
    cursor = r.json()["next_cursor"]
    assert cursor  # 两页快照才有续页游标
    r = _confirm(client, cursor)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_CONFIRM_TOKEN"


def test_confirm_outside_shifts_no_impact(client):
    """候选区间不与任何班次相交（新来源）：影响为空，确认正常落库、无修订。"""
    shift_id = _settled_shift(client)
    cand = ev("NEW-9", 1, "2026-10-05T10:30:00+08:00",
              "2026-10-05T11:30:00+08:00", "failure")
    r = _preview(client, cand)
    assert r.status_code == 200 and r.json()["impacts"] == []
    r = _confirm(client, r.json()["confirm_token"])
    assert r.status_code == 201
    assert r.json()["recomputed"] == []
    assert _result(client, shift_id)["revision"] == 1


def test_confirm_concurrent_same_token(client):
    """并发确认同一凭据：一个新建、一个幂等，版本链与修订不重不漏。"""
    shift_id = _settled_shift(client)
    token = _preview(client, E_FAIL_V2).json()["confirm_token"]
    results = []

    def worker():
        r = _confirm(client, token)
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(c for c, _ in results)
    assert codes == [200, 201]
    assert _result(client, shift_id)["revision"] == 2
    assert len(_changes(client)["changes"]) == 2
    # 链上只有 v2 一个版本：再次确认仍幂等
    r = _confirm(client, token)
    assert r.status_code == 200 and r.json()["idempotent"] is True
