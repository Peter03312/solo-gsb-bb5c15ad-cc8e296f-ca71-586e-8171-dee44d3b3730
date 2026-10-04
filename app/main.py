"""HTTP API 入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from . import services
from .db import get_db, init_db
from .models import Shift, ShiftResult
from .schemas import (
    AdjudicationAck,
    AdjudicationIn,
    AdjudicationOut,
    ChangeEntry,
    ChangePage,
    DeviceCreate,
    DeviceOut,
    EventAccepted,
    EventBatchAccepted,
    EventBatchIn,
    EventBatchItemAck,
    EventConfirmIn,
    EventIn,
    EventPreviewOut,
    EventVersionRef,
    ReasonMinutes,
    RecomputedShift,
    ReconciledShiftRef,
    ReconciliationIn,
    ReconciliationInterval,
    ReconciliationOut,
    ResultOut,
    ResultSegmentOut,
    ResultSegmentsOut,
    RevisionRef,
    SettleResponse,
    ShiftCreate,
    ShiftImpactPreview,
    ShiftOut,
    ShiftSnapshotEntry,
    ShiftSnapshotPage,
    _check_ts,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 建库建表（含排他约束），并等待数据库就绪
    yield


app = FastAPI(
    title="设备停机归因服务",
    version="1.7.0",
    description=(
        "按班次对设备停机时间进行归因（保养/故障/待判）。"
        "区间均为半开 [start, end)；重叠时保养 > 故障 > 待判，每分钟只归因一次；"
        "事件按 (来源, 连续版本) 上报，支持幂等与迟到更正重算；"
        "上报前可经 POST /events/preview 只读预演候选版本对已结算班次的影响，"
        "再凭确认凭据经 POST /events/confirm 原子提交（状态已变则拒绝并要求重新预演）；"
        "现场网关断网恢复后可经 POST /events/batch 整批补传同一设备的事件列表"
        "（全或无：逐项核对版本链，任一非法整批失败）；"
        "已结算班次支持现场人工裁定（提交/按单号撤销），裁定覆盖自动事件归因，"
        "撤销后回退；仅当各原因分钟数变化时追加班次修订并进入全局游标变更流；"
        "GET /changes 提供按全局递增游标分页的结算/更正/裁定变更流，供下游报表同步；"
        "GET /devices/{device_id}/shift-snapshots 提供按固定变更序号的班次归因快照，"
        "供下游报表首次接入或重建索引；"
        "GET /shifts/{shift_id}/result/segments 提供与修订同事务固化的不可变归因区间"
        "明细（按起点排序、互不重叠的半开区间，含起止、原因及依据），"
        "历史修订未固化明细时明确返回不可用；"
        "POST /devices/{device_id}/downtime-reconciliation 按固定变更序号对账现场"
        "停机记录与系统已结算归因（仅在已结算班次覆盖分钟比较，差异与未对账"
        "分钟分别给出最大连续区间，结果携带所用序号以便重放）。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """统一参数校验错误格式（非法区间、缺时区、未对齐整分钟等）。"""
    errors = exc.errors()
    message = "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', []) if p != 'body')}: {e.get('msg')}"
        .lstrip(": ")
        for e in errors
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "VALIDATION_ERROR",
                "message": message or "invalid request",
                "errors": jsonable_encoder(errors, custom_encoder={Exception: str}),
            }
        },
    )


def _result_out(shift: Shift, rev: ShiftResult, latest_revision: int) -> ResultOut:
    return ResultOut(
        shift_id=shift.id,
        device_id=shift.device_id,
        shift_start=shift.start_ts,
        shift_end=shift.end_ts,
        revision=rev.revision,
        latest_revision=latest_revision,
        totals=ReasonMinutes(
            maintenance=rev.maintenance_minutes,
            failure=rev.failure_minutes,
            pending=rev.pending_minutes,
        ),
        delta=ReasonMinutes(
            maintenance=rev.d_maintenance,
            failure=rev.d_failure,
            pending=rev.d_pending,
        ),
        event_versions=[EventVersionRef(**ev) for ev in rev.event_versions],
        trigger=rev.trigger,
        created_at=rev.created_at,
    )


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 设备


@app.post("/devices", response_model=DeviceOut, tags=["devices"])
def create_device(
    payload: DeviceCreate, response: Response, db: Session = Depends(get_db)
):
    """注册设备；重复提交同 id 设备幂等返回（200）。"""
    device, created = services.create_device(db, payload)
    response.status_code = 201 if created else 200
    return DeviceOut(id=device.id, name=device.name)


@app.get("/devices", response_model=list[DeviceOut], tags=["devices"])
def list_devices(db: Session = Depends(get_db)):
    return [DeviceOut(id=d.id, name=d.name) for d in services.list_devices(db)]


# ---------------------------------------------------------------- 班次


@app.post("/shifts", response_model=ShiftOut, status_code=201, tags=["shifts"])
def create_shift(payload: ShiftCreate, db: Session = Depends(get_db)):
    """创建班次（半开区间，同设备不可重叠）。"""
    shift = services.create_shift(db, payload)
    return ShiftOut(
        id=shift.id,
        device_id=shift.device_id,
        start=shift.start_ts,
        end=shift.end_ts,
    )


@app.get("/shifts/{shift_id}", response_model=ShiftOut, tags=["shifts"])
def get_shift(shift_id: int, db: Session = Depends(get_db)):
    shift = services.get_shift_or_404(db, shift_id)
    return ShiftOut(
        id=shift.id,
        device_id=shift.device_id,
        start=shift.start_ts,
        end=shift.end_ts,
    )


@app.post(
    "/shifts/{shift_id}/settle", response_model=SettleResponse, tags=["attribution"]
)
def settle_shift(shift_id: int, db: Session = Depends(get_db)):
    """结算班次：按当前生效事件计算归因，写入修订 1；重复调用幂等返回最新修订。"""
    shift, rev, created = services.settle_shift(db, shift_id)
    return SettleResponse(
        settled=created, result=_result_out(shift, rev, rev.revision)
    )


# ---------------------------------------------------------------- 事件


def _recomputed_out(
    recomputed: list[tuple[Shift, ShiftResult]],
) -> list[RecomputedShift]:
    return [
        RecomputedShift(
            shift_id=shift.id,
            revision=rev.revision,
            totals=ReasonMinutes(
                maintenance=rev.maintenance_minutes,
                failure=rev.failure_minutes,
                pending=rev.pending_minutes,
            ),
            delta=ReasonMinutes(
                maintenance=rev.d_maintenance,
                failure=rev.d_failure,
                pending=rev.d_pending,
            ),
        )
        for shift, rev in recomputed
    ]


@app.post("/events", response_model=EventAccepted, tags=["events"])
def ingest_event(
    payload: EventIn, response: Response, db: Session = Depends(get_db)
):
    """上报停机事件版本。

    - 同来源同版本同内容：幂等返回（200，idempotent=true）；
    - 同版本异内容 / 跳号：409；
    - 新版本（含迟到更正）：201，并按事件时间重算受影响的已结算班次，
      在 recomputed 中返回新修订与差额。
    """
    ev, idempotent, recomputed = services.ingest_event(db, payload)
    response.status_code = 200 if idempotent else 201
    return EventAccepted(
        device_id=ev.device_id,
        source_id=ev.source_id,
        version=ev.version,
        idempotent=idempotent,
        recomputed=_recomputed_out(recomputed),
    )


@app.post("/events/batch", response_model=EventBatchAccepted, tags=["events"])
def ingest_event_batch(
    payload: EventBatchIn, response: Response, db: Session = Depends(get_db)
):
    """现场网关断网恢复后的批量补传：同一设备的非空事件列表，整批核对、整批应用。

    - 逐项沿用单条上报的字段与校验，按列表顺序核对每个来源的版本链：
      已存在的同版本同内容项为幂等重试（回执 idempotent=true）；
    - 同版本异内容 409 VERSION_CONFLICT、跳号 409 VERSION_GAP、
      未知设备 404、任一项非法 422 —— 整批失败，不留下事件、修订或变更；
    - 成功时先应用全部新版本，仅以批前和批后各来源最新版本的区间并集
      确定受影响的已结算班次，再按最终状态重算：每班次最多一次修订和
      一条变更，分钟数未变则都不产生；
    - 全部项幂等时整体返回 200，否则 201。
    """
    acks, recomputed = services.ingest_event_batch(db, payload)
    items = [EventBatchItemAck(**a) for a in acks]
    response.status_code = 200 if all(a.idempotent for a in items) else 201
    return EventBatchAccepted(
        device_id=payload.device_id,
        items=items,
        recomputed=_recomputed_out(recomputed),
    )


@app.post("/events/preview", response_model=EventPreviewOut, tags=["events"])
def preview_event(payload: EventIn, db: Session = Depends(get_db)):
    """上报前只读预演：预览候选事件版本对已结算班次的影响。

    - 字段、校验与版本链规则同单条上报：同版本同内容幂等（idempotent=true，
      无影响）、同版本异内容 409 VERSION_CONFLICT、跳号 409 VERSION_GAP、
      未知设备 404、非法输入 422；
    - 以旧版与候选版区间并集找出相关已结算班次，按当前全部来源最新版本及
      生效裁定计算各班次的现有分钟数、拟变分钟数与差额；结果不变的班次也
      列出（changed=false）；
    - 只读：不写入事件、修订或变更；返回的 confirm_token 供
      POST /events/confirm 原样回传以原子提交。
    """
    idempotent, impacts, token = services.preview_event(db, payload)
    return EventPreviewOut(
        device_id=payload.device_id,
        source_id=payload.source_id,
        version=payload.version,
        idempotent=idempotent,
        confirm_token=token,
        impacts=[ShiftImpactPreview(**i) for i in impacts],
    )


@app.post("/events/confirm", response_model=EventAccepted, tags=["events"])
def confirm_event(
    payload: EventConfirmIn, response: Response, db: Session = Depends(get_db)
):
    """凭预演确认凭据原子提交候选事件版本。

    - 重新核对候选内容、来源版本链及影响结果的班次/事件/裁定状态，
      任一已变 → 409 PREVIEW_STALE，不写入任何数据，需重新预演；
    - 状态未变 → 201，同事务落库事件并沿用现有更正规则，仅为分钟数
      变化的班次生成修订与变更（recomputed 中返回）；
    - 相同确认重试遵循现有事件幂等语义（200，idempotent=true）；
      同版本异内容 409 VERSION_CONFLICT、跳号 409 VERSION_GAP；
    - 凭据伪造/篡改/损坏 → 422 INVALID_CONFIRM_TOKEN。
    """
    ev, idempotent, recomputed = services.confirm_event(db, payload.confirm_token)
    response.status_code = 200 if idempotent else 201
    return EventAccepted(
        device_id=ev.device_id,
        source_id=ev.source_id,
        version=ev.version,
        idempotent=idempotent,
        recomputed=_recomputed_out(recomputed),
    )


# ---------------------------------------------------------------- 结果查询


@app.get("/shifts/{shift_id}/result", response_model=ResultOut, tags=["attribution"])
def get_result(
    shift_id: int,
    revision: int | None = Query(default=None, ge=1, description="指定修订号，缺省为最新"),
    db: Session = Depends(get_db),
):
    """查询班次归因结果；可用 ?revision=N 查看历史修订（原结果永久保留）。"""
    shift, rev, latest_revision = services.get_result(db, shift_id, revision)
    return _result_out(shift, rev, latest_revision)


@app.get(
    "/shifts/{shift_id}/revisions", response_model=list[ResultOut], tags=["attribution"]
)
def list_revisions(shift_id: int, db: Session = Depends(get_db)):
    """列出班次的全部修订（含每次更正的差额）。"""
    shift, revs = services.list_revisions(db, shift_id)
    latest_revision = revs[-1].revision if revs else 0
    return [_result_out(shift, rev, latest_revision) for rev in revs]


@app.get(
    "/shifts/{shift_id}/result/segments",
    response_model=ResultSegmentsOut,
    tags=["attribution"],
)
def get_result_segments(
    shift_id: int,
    revision: int | None = Query(default=None, ge=1, description="指定修订号，缺省为最新"),
    db: Session = Depends(get_db),
):
    """查询班次某修订的归因区间明细（解释各原因分钟数的构成）。

    - 返回按起点排序、互不重叠的半开区间段，每段含起止、分钟数、原因及依据
      （生效裁定单号，或该区间内决定此原因的事件来源与版本列表）；
    - 只含停机分钟；各原因段分钟之和等于该修订的对应总数；
    - 明细与修订同事务固化、不可变：事件更正或裁定撤销后旧修订明细保持原样；
    - 本结构引入前生成的历史修订未固化明细：available=false 且 segments=null
      （明确不可用，不以当前状态推算）；
    - 未知班次 404 SHIFT_NOT_FOUND、未结算 404 RESULT_NOT_FOUND、
      未知修订 404 REVISION_NOT_FOUND。
    """
    shift, rev, latest_revision, segments = services.get_result_segments(
        db, shift_id, revision
    )
    if segments is None:
        return ResultSegmentsOut(
            shift_id=shift.id,
            revision=rev.revision,
            latest_revision=latest_revision,
            available=False,
            segments=None,
        )
    return ResultSegmentsOut(
        shift_id=shift.id,
        revision=rev.revision,
        latest_revision=latest_revision,
        available=True,
        segments=[
            ResultSegmentOut(
                start=seg.start_ts,
                end=seg.end_ts,
                minutes=int((seg.end_ts - seg.start_ts).total_seconds()) // 60,
                reason=seg.reason,
                basis=seg.basis,
            )
            for seg in segments
        ],
    )


# ---------------------------------------------------------------- 现场人工裁定


def _adjudication_ack(
    ruling, idempotent: bool, rev: ShiftResult | None, latest_revision: int
) -> AdjudicationAck:
    detail = None
    if rev is not None:
        detail = RevisionRef(
            revision=rev.revision,
            seq=rev.seq,
            totals=ReasonMinutes(
                maintenance=rev.maintenance_minutes,
                failure=rev.failure_minutes,
                pending=rev.pending_minutes,
            ),
            delta=ReasonMinutes(
                maintenance=rev.d_maintenance,
                failure=rev.d_failure,
                pending=rev.d_pending,
            ),
            trigger=rev.trigger,
        )
    return AdjudicationAck(
        ruling_no=ruling.ruling_no,
        status=ruling.status,
        idempotent=idempotent,
        revision=rev.revision if rev is not None else None,
        latest_revision=latest_revision,
        revision_detail=detail,
    )


@app.post(
    "/shifts/{shift_id}/adjudications",
    response_model=AdjudicationAck,
    tags=["adjudications"],
)
def submit_adjudication(
    shift_id: int,
    payload: AdjudicationIn,
    response: Response,
    db: Session = Depends(get_db),
):
    """为已结算班次提交现场人工裁定。

    - 裁定分钟覆盖自动事件归因；重叠分钟按生效顺序最新的未撤销裁定为准，
      撤销后回退至此前有效裁定，无裁定时按当前最新事件版本计算；
    - 同单号同内容重试幂等（200，不产生修订）；同单号异内容 409 RULING_CONFLICT；
    - 仅当各原因分钟数变化时产生班次修订（revision 非空）并进入 /changes；
    - 未知班次 404、班次未结算 409 SHIFT_NOT_SETTLED、区间越界 422 RULING_OUT_OF_RANGE、
      非法原因/时间格式 422 VALIDATION_ERROR。
    """
    _shift, ruling, idempotent, rev, latest = services.submit_adjudication(
        db, shift_id, payload
    )
    response.status_code = 200 if idempotent else 201
    return _adjudication_ack(ruling, idempotent, rev, latest)


@app.post(
    "/shifts/{shift_id}/adjudications/{ruling_no}/revoke",
    response_model=AdjudicationAck,
    tags=["adjudications"],
)
def revoke_adjudication(
    shift_id: int,
    ruling_no: str,
    response: Response,
    db: Session = Depends(get_db),
):
    """按单号撤销现场人工裁定。

    - 首次撤销 201，分钟数变化时产生修订（trigger=adjudication_revoke）；
    - 重复撤销幂等（200，不产生修订）；
    - 未知班次 404 SHIFT_NOT_FOUND，未知单号 404 RULING_NOT_FOUND。
    """
    _shift, ruling, idempotent, rev, latest = services.revoke_adjudication(
        db, shift_id, ruling_no
    )
    response.status_code = 200 if idempotent else 201
    return _adjudication_ack(ruling, idempotent, rev, latest)


@app.get(
    "/shifts/{shift_id}/adjudications",
    response_model=list[AdjudicationOut],
    tags=["adjudications"],
)
def list_adjudications(shift_id: int, db: Session = Depends(get_db)):
    """列出班次的全部现场人工裁定（含已撤销），按生效顺序排列。"""
    _shift, rows = services.list_adjudications(db, shift_id)
    return [
        AdjudicationOut(
            ruling_no=r.ruling_no,
            start=r.start_ts,
            end=r.end_ts,
            reason=r.reason,
            note=r.note,
            status=r.status,
            created_at=r.created_at,
            revoked_at=r.revoked_at,
        )
        for r in rows
    ]


# ---------------------------------------------------------------- 变更同步


def _change_entry(shift: Shift, rev: ShiftResult) -> ChangeEntry:
    return ChangeEntry(
        seq=rev.seq,
        shift_id=shift.id,
        device_id=shift.device_id,
        shift_start=shift.start_ts,
        shift_end=shift.end_ts,
        revision=rev.revision,
        totals=ReasonMinutes(
            maintenance=rev.maintenance_minutes,
            failure=rev.failure_minutes,
            pending=rev.pending_minutes,
        ),
        delta=ReasonMinutes(
            maintenance=rev.d_maintenance,
            failure=rev.d_failure,
            pending=rev.d_pending,
        ),
        event_versions=[EventVersionRef(**ev) for ev in rev.event_versions],
        trigger=rev.trigger,
        created_at=rev.created_at,
    )


@app.get("/changes", response_model=ChangePage, tags=["sync"])
def list_changes(
    cursor: int = Query(
        default=0, ge=0, description="已消费的最大变更序号；0（缺省）表示从头回放"
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=services.MAX_PAGE_SIZE,
        description=f"每页条数，上限 {services.MAX_PAGE_SIZE}",
    ),
    db: Session = Depends(get_db),
):
    """下游报表同步：按全局递增游标分页读取每次结算/更正产生的变更。

    - 每条含班次、修订号、各原因分钟数、差额与触发类型；
    - 修订与变更记录同事务提交/回滚；序号顺序即提交顺序，
      并发结算/更正时游标不会越过尚未可见的较早变更；
    - 已返回条目不可变，重读内容不变；空页保持原游标；
    - 非法游标（负数、超过当前最大序号）或超限页大小返回 422。
    """
    page, next_cursor = services.list_changes(db, cursor, limit)
    return ChangePage(
        changes=[_change_entry(shift, rev) for rev, shift in page],
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------- 班次归因快照


def _snapshot_entry(shift: Shift, rev: ShiftResult) -> ShiftSnapshotEntry:
    return ShiftSnapshotEntry(
        shift_id=shift.id,
        start=shift.start_ts,
        end=shift.end_ts,
        revision=rev.revision,
        totals=ReasonMinutes(
            maintenance=rev.maintenance_minutes,
            failure=rev.failure_minutes,
            pending=rev.pending_minutes,
        ),
    )


@app.get(
    "/devices/{device_id}/shift-snapshots",
    response_model=ShiftSnapshotPage,
    tags=["sync"],
)
def get_shift_snapshot(
    device_id: str,
    start: datetime = Query(
        description="半开时间窗口起点（含），必须带时区且精确到整分钟，如 2026-10-01T00:00:00+08:00"
    ),
    end: datetime = Query(
        description="半开时间窗口终点（不含），必须带时区且精确到整分钟"
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=services.MAX_SNAPSHOT_PAGE_SIZE,
        description=f"每页条数，上限 {services.MAX_SNAPSHOT_PAGE_SIZE}",
    ),
    seq: int | None = Query(
        default=None,
        ge=0,
        description="快照变更序号；缺省时以本次请求读取时已提交的最大序号为准",
    ),
    cursor: str | None = Query(
        default=None, description="上一页返回的不透明续页游标；首页不传"
    ),
    db: Session = Depends(get_db),
):
    """下游报表首次接入 / 重建索引：按固定变更序号读取班次归因快照。

    - 只列出与半开窗口 [start, end) 相交、截至快照序号**已结算**的班次；
      每班次取 seq ≤ 快照序号的最后修订，返回班次标识、区间、修订号与三类原因分钟数；
    - 未指定 seq 时以首次请求读取时已提交的最大变更序号为准；每页回传该序号，
      可直接作为 GET /changes 的起始游标继续增量同步；
    - 按班次标识（id）稳定升序翻页；快照只读不可变历史，后续更正、裁定或新结算
      不改变同一快照的后续页；末页 next_cursor 为 null，空快照返回空页；
    - 超过当前已提交范围的序号 → 422 INVALID_SNAPSHOT_SEQ；
      与筛选条件或序号不匹配、伪造或损坏的续页游标 → 422 INVALID_CURSOR；
      非法窗口（缺时区、未对齐整分钟、start ≥ end）或页大小 → 422 VALIDATION_ERROR；
      未知设备 → 404 DEVICE_NOT_FOUND。
    """
    try:
        _check_ts(start)
        _check_ts(end)
        if start >= end:
            raise ValueError(
                "invalid window: start must be earlier than end (half-open [start, end))"
            )
    except ValueError as exc:
        raise services.ApiError(422, "VALIDATION_ERROR", str(exc))

    page, snapshot_seq, next_cursor = services.get_shift_snapshot(
        db, device_id, start, end, seq, limit, cursor
    )
    return ShiftSnapshotPage(
        device_id=device_id,
        start=start,
        end=end,
        snapshot_seq=snapshot_seq,
        shifts=[_snapshot_entry(shift, rev) for shift, rev in page],
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------- 现场停机对账（按设备）


def _reconciliation_intervals(intervals: list) -> list[ReconciliationInterval]:
    return [
        ReconciliationInterval(
            start=s,
            end=e,
            minutes=int((e - s).total_seconds()) // 60,
        )
        for s, e in intervals
    ]


@app.post(
    "/devices/{device_id}/downtime-reconciliation",
    response_model=ReconciliationOut,
    tags=["reconciliation"],
)
def reconcile_downtime(device_id: str, payload: ReconciliationIn, db: Session = Depends(get_db)):
    """按设备对账现场停机记录与系统已结算归因（固定变更序号，可重放）。

    - 现场区间先裁剪到半开窗口 [start, end) 并合并重叠；系统侧按该序号选取
      各相交班次的**最后修订**及其固化归因明细，三类归因段（保养/故障/待判）
      的并集代表系统停机；
    - 仅在该序号下已结算班次覆盖的分钟内比较：按时间顺序返回现场有而系统无
      （field_only）、系统有而现场无（system_only）的最大连续区间及分钟总数；
      不在已结算班次内的现场分钟另计 unreconciled（未对账，不算差异）；
    - seq 缺省时固定为请求开始时已提交的最大变更序号，结果回传该序号以便重放；
    - seq 超过当前最大序号 → 422 INVALID_SEQ；所选修订固化明细不可用
      （结构引入前的历史修订）→ 422 SEGMENTS_UNAVAILABLE（不以当前结果替代）；
      未知设备 → 404 DEVICE_NOT_FOUND；窗口/区间缺时区、未对齐整分钟或
      start ≥ end → 422 VALIDATION_ERROR。
    """
    result = services.reconcile_device_downtime(
        db,
        device_id,
        payload.start,
        payload.end,
        [(iv.start, iv.end) for iv in payload.field_intervals],
        payload.seq,
    )
    return ReconciliationOut(
        device_id=result["device_id"],
        start=result["start"],
        end=result["end"],
        seq=result["seq"],
        settled_minutes=result["settled_minutes"],
        field_only=_reconciliation_intervals(result["field_only"]),
        field_only_minutes=result["field_only_minutes"],
        system_only=_reconciliation_intervals(result["system_only"]),
        system_only_minutes=result["system_only_minutes"],
        unreconciled=_reconciliation_intervals(result["unreconciled"]),
        unreconciled_minutes=result["unreconciled_minutes"],
        reconciled_shifts=[ReconciledShiftRef(**ref) for ref in result["reconciled_shifts"]],
    )
