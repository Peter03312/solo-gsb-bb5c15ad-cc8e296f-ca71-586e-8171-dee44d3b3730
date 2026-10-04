"""Pydantic 请求/响应模型。

时间戳规则：必须带时区且精确到整分钟（秒与微秒为 0），否则 422。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# 保养 / 故障 / 待判
Reason = Literal["maintenance", "failure", "pending"]


def _check_ts(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError(
            "timestamp must include a timezone offset, e.g. 2026-10-01T08:00:00+08:00"
        )
    if v.second != 0 or v.microsecond != 0:
        raise ValueError("timestamp must be aligned to a whole minute (seconds must be 00)")
    return v


class _Interval(BaseModel):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        return _check_ts(v)

    @model_validator(mode="after")
    def _range(self):
        if not self.start < self.end:
            raise ValueError("invalid interval: start must be earlier than end")
        return self


class DeviceCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64, examples=["CNC-01"])
    name: str = Field(default="", max_length=200)


class DeviceOut(BaseModel):
    id: str
    name: str


class ShiftCreate(_Interval):
    device_id: str = Field(min_length=1, max_length=64)


class ShiftOut(BaseModel):
    id: int
    device_id: str
    start: datetime
    end: datetime


class EventIn(_Interval):
    device_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    reason: Reason


class EventBatchItem(_Interval):
    """断网补传的单个事件版本：字段与校验同单条上报（设备在批次级给出）。"""

    source_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    reason: Reason


class EventBatchIn(BaseModel):
    """现场网关断网恢复后的一次性补传：同一设备的非空事件列表。"""

    device_id: str = Field(min_length=1, max_length=64)
    events: list[EventBatchItem] = Field(min_length=1)


class ReasonMinutes(BaseModel):
    maintenance: int
    failure: int
    pending: int


class EventVersionRef(BaseModel):
    source_id: str
    version: int
    reason: str


class RecomputedShift(BaseModel):
    """迟到更正触发重算后，某个受影响班次的新修订摘要。"""

    shift_id: int
    revision: int
    totals: ReasonMinutes
    delta: ReasonMinutes


class EventAccepted(BaseModel):
    device_id: str
    source_id: str
    version: int
    idempotent: bool  # True 表示同来源同版本同内容的重复提交，未产生新数据
    recomputed: list[RecomputedShift] = []


class EventBatchItemAck(BaseModel):
    """批量补传的逐项回执：idempotent=True 表示同版本同内容的已存在项（重试）。"""

    source_id: str
    version: int
    idempotent: bool


class EventBatchAccepted(BaseModel):
    """批量补传回执：逐项标明新建/幂等，并列出实际修订的班次及差额。"""

    device_id: str
    items: list[EventBatchItemAck]
    recomputed: list[RecomputedShift] = []


# ---------------------------------------------------------------- 事件预演与确认


class ShiftImpactPreview(BaseModel):
    """预演中某个相关已结算班次的分钟数对比（结果不变的班次也列出）。"""

    shift_id: int
    revision: int  # 班次当前最新修订号
    current: ReasonMinutes  # 现有分钟数（当前最新修订）
    proposed: ReasonMinutes  # 拟变分钟数（应用候选版本后）
    delta: ReasonMinutes  # 差额 = 拟变 − 现有
    changed: bool  # 各原因分钟数是否有变化（False 的班次确认后不会产生修订）


class EventPreviewOut(BaseModel):
    """事件预演回执：只读预演，不写入事件、修订或变更。

    confirm_token 为不透明确认凭据（绑定候选内容与预演时的状态指纹），
    供 POST /events/confirm 原样回传；idempotent=True 表示同来源同版本
    同内容已存在，确认将按现有幂等语义返回且无影响（impacts 为空）。
    """

    device_id: str
    source_id: str
    version: int
    idempotent: bool
    confirm_token: str
    impacts: list[ShiftImpactPreview] = []


class EventConfirmIn(BaseModel):
    """确认提交：凭预演返回的确认凭据原子提交候选事件版本。"""

    # 凭据大小随相关已结算班次数增长（每班约百字节），上限留足余量
    confirm_token: str = Field(min_length=1, max_length=65536)


class ResultOut(BaseModel):
    shift_id: int
    device_id: str
    shift_start: datetime
    shift_end: datetime
    revision: int
    latest_revision: int
    totals: ReasonMinutes
    delta: ReasonMinutes  # 本修订相对上一修订的差额
    event_versions: list[EventVersionRef]
    trigger: str  # settle | correction
    created_at: datetime


class SettleResponse(BaseModel):
    settled: bool  # False 表示此前已结算，本次为幂等返回最新修订
    result: ResultOut


# ---------------------------------------------------------------- 归因区间明细


class SegmentBasisEvents(BaseModel):
    """事件归因依据：该区间内决定此原因的事件来源与版本列表。"""

    type: Literal["events"] = "events"
    event_versions: list[EventVersionRef]


class SegmentBasisRuling(BaseModel):
    """裁定依据：决定该段原因的生效裁定单号。"""

    type: Literal["ruling"] = "ruling"
    ruling_no: str


#: 归因区间段的依据（按 type 判别）
SegmentBasis = Annotated[
    SegmentBasisEvents | SegmentBasisRuling, Field(discriminator="type")
]


class ResultSegmentOut(BaseModel):
    """一段归因区间明细：半开区间 [start, end)，含起止、原因及依据。"""

    start: datetime
    end: datetime
    minutes: int  # 段分钟数 = (end - start)，便于直接汇总
    reason: str  # maintenance | failure | pending
    basis: SegmentBasis


class ResultSegmentsOut(BaseModel):
    """班次某修订的归因区间明细。

    - segments 按起点排序、互不重叠；只含停机分钟（未停机分钟不返回），
      各原因段分钟之和等于该修订的对应总数；
    - 明细与修订同事务固化、不可变：事件更正或裁定撤销只影响之后的新修订；
    - 本结构引入前生成的历史修订未固化明细（当时的裁定状态无法可靠还原），
      available=False 且 segments 为 null —— 明确不可用，不以当前状态推算。
    """

    shift_id: int
    revision: int
    latest_revision: int
    available: bool  # False 表示历史修订无明细（segments 为 null）
    segments: list[ResultSegmentOut] | None


class ChangeEntry(BaseModel):
    """一次结算/更正/裁定产生的变更记录（与班次修订同一行存储，内容不可变）。"""

    seq: int  # 全局递增变更序号（提交顺序），作为同步游标
    shift_id: int
    device_id: str
    shift_start: datetime
    shift_end: datetime
    revision: int
    totals: ReasonMinutes
    delta: ReasonMinutes  # 本修订相对上一修订的差额
    event_versions: list[EventVersionRef]
    trigger: str  # settle | correction | adjudication | adjudication_revoke
    created_at: datetime


class ChangePage(BaseModel):
    """一页变更；next_cursor 供下一页使用，空页保持原游标。"""

    changes: list[ChangeEntry]
    next_cursor: int


# ---------------------------------------------------------------- 班次归因快照


class ShiftSnapshotEntry(BaseModel):
    """固定变更序号下班次的归因快照条目（截至该序号的最后修订，内容不可变）。"""

    shift_id: int
    start: datetime  # 班次区间起点（半开 [start, end)，UTC 输出）
    end: datetime
    revision: int  # 序号不大于快照序号的最后修订号
    totals: ReasonMinutes  # 该修订的保养/故障/待判分钟数


class ShiftSnapshotPage(BaseModel):
    """一页班次快照。

    - snapshot_seq 全部分页保持不变；每页回传，可直接作为 GET /changes 的起始游标；
    - next_cursor 为不透明续页游标，末页为 null（null 即翻页结束）；
    - 空快照（无任何匹配班次）首页即空页，next_cursor 为 null。
    """

    device_id: str
    start: datetime
    end: datetime
    snapshot_seq: int
    shifts: list[ShiftSnapshotEntry]
    next_cursor: str | None


# ---------------------------------------------------------------- 现场人工裁定


class AdjudicationIn(_Interval):
    """已结算班次的现场人工裁定：班次内整分钟半开区间 + 原因 + 说明。"""

    ruling_no: str = Field(
        min_length=1,
        max_length=64,
        description="唯一裁定单号（班次内唯一，建议全局唯一），用于幂等与撤销",
        examples=["R-20261001-001"],
    )
    reason: Reason
    note: str = Field(default="", max_length=500, description="说明（可选）")


class AdjudicationOut(BaseModel):
    """一条现场人工裁定的当前状态（撤销留痕）。"""

    ruling_no: str
    start: datetime
    end: datetime
    reason: str
    note: str
    status: str  # active | revoked
    created_at: datetime
    revoked_at: datetime | None = None


class RevisionRef(BaseModel):
    """操作产生的班次修订摘要；未产生修订时该对象为空（response 中为 null）。"""

    revision: int
    seq: int
    totals: ReasonMinutes
    delta: ReasonMinutes
    trigger: str


# ---------------------------------------------------------------- 现场停机对账（按设备）


class FieldIntervalIn(_Interval):
    """现场停机区间（整分钟、半开）；可与窗口部分相交，服务端裁剪到窗口后合并。"""


class ReconciliationIn(BaseModel):
    """按设备对账请求：窗口 + 现场停机区间列表 + 可选固定变更序号。"""

    start: datetime
    end: datetime
    field_intervals: list[FieldIntervalIn] = Field(
        default_factory=list, description="现场停机区间列表（整分钟半开）；裁剪到窗口后合并重叠"
    )
    seq: int | None = Field(
        default=None,
        ge=0,
        description="固定变更序号；缺省时以请求开始时已提交的最大序号为准",
    )

    @field_validator("start", "end")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        return _check_ts(v)

    @model_validator(mode="after")
    def _range(self):
        if not self.start < self.end:
            raise ValueError("invalid window: start must be earlier than end")
        return self


class ReconciliationInterval(BaseModel):
    """对账结果中的最大连续区间（半开，UTC 输出）及分钟数。"""

    start: datetime
    end: datetime
    minutes: int


class ReconciledShiftRef(BaseModel):
    """对账所用的各已结算班次及其固定序号下选取的修订（便于重放核对）。"""

    shift_id: int
    revision: int
    seq: int  # 该修订的全局变更序号（均 ≤ 对账所用序号）


class ReconciliationOut(BaseModel):
    """设备停机对账结果。

    - field_only：已结算覆盖分钟内现场有、系统无的最大连续区间（差异）；
    - system_only：系统有、现场无的最大连续区间（差异）；
    - unreconciled：现场记录落在已结算班次之外的分钟（未对账，不算差异）；
    - seq：本次实际使用的变更序号（未指定时固定为请求开始时的最新序号），可凭此重放。
    """

    device_id: str
    start: datetime
    end: datetime
    seq: int
    settled_minutes: int  # 窗口内已结算班次覆盖的分钟总数（对账范围）
    field_only: list[ReconciliationInterval]
    field_only_minutes: int
    system_only: list[ReconciliationInterval]
    system_only_minutes: int
    unreconciled: list[ReconciliationInterval]
    unreconciled_minutes: int
    reconciled_shifts: list[ReconciledShiftRef]


class AdjudicationAck(BaseModel):
    """提交/撤销裁定的统一回执：单号、状态、修订号（无修订时明确标示）。"""

    ruling_no: str
    status: str  # active | revoked
    idempotent: bool  # True 表示同单号重试/重复撤销，未产生任何修订
    revision: int | None  # 本次操作产生的修订号；无修订为 null
    latest_revision: int  # 班次当前最新修订号
    revision_detail: RevisionRef | None = None  # 本次修订摘要；无修订为 null
