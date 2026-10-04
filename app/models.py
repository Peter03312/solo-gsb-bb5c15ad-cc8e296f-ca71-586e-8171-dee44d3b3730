"""SQLAlchemy 数据模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Shift(Base):
    """班次：半开区间 [start_ts, end_ts)，同设备不得重叠（由排他约束兜底）。"""

    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id"), nullable=False, index=True
    )
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("end_ts > start_ts", name="ck_shifts_valid_range"),
    )


class EventSource(Base):
    """每个 (设备, 来源) 的版本链游标，用于幂等校验与连续版本控制。"""

    __tablename__ = "event_sources"

    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_content_hash: Mapped[str | None] = mapped_column(String(64))


class EventVersion(Base):
    """停机事件的某个版本；同一来源的最新版本为当前生效事件。"""

    __tablename__ = "event_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("device_id", "source_id", "version", name="uq_event_version"),
        CheckConstraint("end_ts > start_ts", name="ck_events_valid_range"),
        CheckConstraint(
            "reason IN ('maintenance','failure','pending')", name="ck_events_reason"
        ),
    )


class ChangeCounter(Base):
    """变更序号计数器（单例行 id=1）。

    分配序号时在写事务内对该行 UPDATE ... RETURNING 递增，行锁持有至提交，
    因此序号顺序 == 提交顺序：seq 较小的变更不会晚于 seq 较大的变更可见，
    下游同步游标据此不会越过尚未提交的较早变更。
    """

    __tablename__ = "change_counter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 恒为 1
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ShiftResult(Base):
    """班次结算结果的一个修订。

    结算后原修订永久保留；迟到更正 / 现场人工裁定 / 撤销裁定触发重算时追加新修订
    并记录与上一修订的差额。
    seq 是全局递增的变更序号（提交顺序），下游按游标同步即读此表 ——
    修订与其变更记录是同一行，天然同事务提交/回滚，且内容不可变。
    """

    __tablename__ = "shift_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # 全局递增变更序号（提交顺序），作为下游同步游标
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    maintenance_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    pending_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    # 与上一修订的差额（首修订相对 0）
    d_maintenance: Mapped[int] = mapped_column(Integer, nullable=False)
    d_failure: Mapped[int] = mapped_column(Integer, nullable=False)
    d_pending: Mapped[int] = mapped_column(Integer, nullable=False)
    # 本次计算所依据的事件版本：[{"source_id", "version", "reason"}, ...]
    event_versions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # settle | correction | adjudication | adjudication_revoke
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    # 是否固化了归因区间明细（shift_result_segments）。
    # 本结构引入前生成的历史修订为 False：当时的裁定状态无法可靠还原，
    # 查询明细时明确返回不可用，不得以当前状态推算。
    segments_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("shift_id", "revision", name="uq_shift_result_revision"),
        UniqueConstraint("seq", name="uq_shift_results_seq"),
    )


class ShiftResultSegment(Base):
    """班次修订的归因区间明细：与修订同事务写入，之后不可变（只插入、不更新）。

    - 每段为半开区间 [start_ts, end_ts)，按 seg_no（即起点升序）排列、互不重叠；
    - 只记录停机分钟（被事件或生效裁定覆盖的分钟），未停机分钟不产生段；
    - basis 为 JSONB，两种形态：
        {"type": "ruling", "ruling_no": "..."}                    —— 生效裁定单号
        {"type": "events", "event_versions": [{"source_id", "version", "reason"}, ...]}
          —— 该段内决定此原因的事件来源与版本列表
    - 事件更正或裁定撤销只追加新修订的明细，历史修订的明细保持原样。
    """

    __tablename__ = "shift_result_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shift_id: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # 段序号（按起点升序，从 1 开始）
    seg_no: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    basis: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["shift_id", "revision"],
            ["shift_results.shift_id", "shift_results.revision"],
            name="fk_segment_result",
        ),
        UniqueConstraint(
            "shift_id", "revision", "seg_no", name="uq_shift_result_segment"
        ),
        CheckConstraint("end_ts > start_ts", name="ck_segments_valid_range"),
        CheckConstraint(
            "reason IN ('maintenance','failure','pending')", name="ck_segments_reason"
        ),
    )


class ManualAdjudication(Base):
    """已结算班次的现场人工裁定。

    - ruling_no 为调用方提供的唯一裁定单号（班次内唯一，建议全局唯一）；
    - 裁定分钟覆盖自动事件归因，重叠分钟按生效顺序（自增 id，即落库顺序）
      最新的未撤销裁定为准；撤销后回退到此前仍有效的裁定或事件归因；
    - 撤销通过将 status 置为 revoked 完成（留痕、支持重复撤销幂等），
      行本身不删除、历史修订不可改写。
    """

    __tablename__ = "manual_adjudications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id"), nullable=False, index=True
    )
    ruling_no: Mapped[str] = mapped_column(String(64), nullable=False)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # active（生效中） | revoked（已撤销）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("shift_id", "ruling_no", name="uq_adjudication_no"),
        CheckConstraint("end_ts > start_ts", name="ck_adjudications_valid_range"),
        CheckConstraint(
            "reason IN ('maintenance','failure','pending')",
            name="ck_adjudications_reason",
        ),
        CheckConstraint(
            "status IN ('active','revoked')", name="ck_adjudications_status"
        ),
    )


class RateVersion(Base):
    """设备停机损失费率表的一个已发布完整版本。

    - 版本号按设备从 1 开始连续递增，由发布事务在设备行锁 + 版本号唯一约束
      下原子分配；一次发布是"全或无"：所有费率行与版本头同事务提交，
      发布后整版（头与行）不可变、不可删改，后续发布只产生新版本号；
    - 核算只引用已提交的版本（共享锁固定一个完整版本），绝不读到半成品版本。
    """

    __tablename__ = "rate_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("device_id", "version", name="uq_rate_version"),
        CheckConstraint("version >= 1", name="ck_rate_version_positive"),
    )


class RateEntry(Base):
    """费率表某版本中的一行费率。

    - reason 在 [start_ts, end_ts)（带时区整分钟半开）内按 amount_per_minute
      （非负定点金额，NUMERIC(18,6)，单位"元/分钟"）计费；
    - 同一版本同一原因的生效区间不得重叠（应用层校验 + 数据库排他约束兜底，
      端点相接不算重叠）；不同原因的区间允许重叠；
    - 行只在发布事务内与其版本头同事务插入，之后不可变。
    """

    __tablename__ = "rate_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 行序号：发布时按 (起点, 终点, 原因) 排序，从 1 开始；核算明细按此回显
    entry_no: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_per_minute: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["device_id", "version"],
            ["rate_versions.device_id", "rate_versions.version"],
            name="fk_rate_entry_version",
        ),
        UniqueConstraint(
            "device_id", "version", "entry_no", name="uq_rate_entry_no"
        ),
        CheckConstraint("end_ts > start_ts", name="ck_rate_entries_valid_range"),
        CheckConstraint(
            "reason IN ('maintenance','failure','pending')",
            name="ck_rate_entries_reason",
        ),
        CheckConstraint(
            "amount_per_minute >= 0", name="ck_rate_entries_amount_nonneg"
        ),
    )


class ShiftLossAccounting(Base):
    """停机损失核算的幂等固化账：按 (班次, 归因修订, 费率版本) 唯一。

    - 对已结算班次指定修订与费率版本核算：用该修订固化的归因段按费率边界
      切分，逐段定点计算并按分四舍五入后汇总；未匹配费率的分钟单列待定价；
    - 核算在该 (修订, 费率版本) 组合上幂等：同组合重复请求返回同一行已固化
      结果（idempotent=true），绝不重算改写；
    - 费率发布新版本或归因更正产生新修订后，只可能产生新组合的新账；
      历史账行与明细只插入、不更新不删除。
    - totals 为 JSONB：{total_amount, reason_amounts, reason_minutes,
      priced_minutes, unpriced_minutes, unpriced_reason_minutes}
    """

    __tablename__ = "shift_loss_accountings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shift_id: Mapped[int] = mapped_column(
        ForeignKey("shifts.id"), nullable=False, index=True
    )
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    totals: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["shift_id", "revision"],
            ["shift_results.shift_id", "shift_results.revision"],
            name="fk_loss_result",
        ),
        ForeignKeyConstraint(
            ["device_id", "rate_version"],
            ["rate_versions.device_id", "rate_versions.version"],
            name="fk_loss_rate_version",
        ),
        UniqueConstraint(
            "shift_id",
            "revision",
            "rate_version",
            name="uq_loss_accounting_combo",
        ),
    )


class ShiftLossAccountingLine(Base):
    """损失账的切分明细：与账头同事务固化，之后不可变（只插入）。

    - 每行一个原因与费率（或待定价）恒定的最大连续半开区间，按 line_no
      （即起点升序）排列、互不重叠；
    - priced=FALSE 时 amount_per_minute 与 rate_entry_no 为空、amount 记 0.00，
      但语义是"未定价"而非零金额（待定价分钟另在账头汇总并返回待定价区间）。
    """

    __tablename__ = "shift_loss_accounting_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shift_id: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    priced: Mapped[bool] = mapped_column(Boolean, nullable=False)
    amount_per_minute: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    # 已按分四舍五入的段金额（待定价行记 0.00）
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    rate_entry_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_start_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rate_end_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["shift_id", "revision", "rate_version"],
            [
                "shift_loss_accountings.shift_id",
                "shift_loss_accountings.revision",
                "shift_loss_accountings.rate_version",
            ],
            name="fk_loss_line_accounting",
        ),
        UniqueConstraint(
            "shift_id",
            "revision",
            "rate_version",
            "line_no",
            name="uq_loss_accounting_line",
        ),
        CheckConstraint("end_ts > start_ts", name="ck_loss_lines_valid_range"),
        CheckConstraint("minutes > 0", name="ck_loss_lines_minutes_pos"),
        CheckConstraint(
            "reason IN ('maintenance','failure','pending')",
            name="ck_loss_lines_reason",
        ),
        CheckConstraint(
            "(priced AND amount_per_minute IS NOT NULL AND rate_entry_no IS NOT NULL) "
            "OR (NOT priced AND amount_per_minute IS NULL AND rate_entry_no IS NULL)",
            name="ck_loss_lines_priced_shape",
        ),
    )
