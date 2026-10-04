"""SQLAlchemy 数据模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
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
    """按设备发布的停机损失费率版本（版本号设备内连续递增，发布原子）。

    - 版本号在发布事务内锁定设备行后分配（1 起连续），版本与其全部费率行
      同一事务提交/回滚：其他事务只能读到完整发布的版本，读不到半成品；
    - 发布后不可变（只追加、不更新不删除）：后续费率发布只产生新版本，
      历史核算结果引用的版本内容永远不变。
    """

    __tablename__ = "rate_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("device_id", "version", name="uq_rate_version"),
    )


class RateEntry(Base):
    """费率版本的一行：某原因在整分钟半开区间 [start_ts, end_ts) 内的
    非负每分钟金额（定点微元，1 元 = 10^6 微元）。

    同版本同原因的区间互不重叠（应用层校验 + btree_gist 排他约束兜底，
    端点相接允许）；不同原因可重叠。行随版本发布后不可变。
    """

    __tablename__ = "rate_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 行内序号（版本内按起点升序，从 1 开始），保证发布内容可读回且顺序稳定
    entry_no: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    # 每分钟金额（微元，1 元 = 10^6）：非负定点数，避免浮点误差
    amount_per_minute: Mapped[int] = mapped_column(BigInteger, nullable=False)

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
            "amount_per_minute >= 0", name="ck_rate_entries_non_negative"
        ),
    )


class LossAccounting(Base):
    """按 (班次, 修订, 费率版本) 幂等固化的停机损失账（账头）。

    - 核算只使用修订**同事务固化的归因段**与该费率版本的不可变费率行，
      账头与全部切分明细同一事务写入；同组合重复核算幂等返回既有账；
    - 后续费率发布或归因更正只产生新组合的新账，历史账行只追加、永不重写；
    - device_id 冗余自习班属（写路径在班次行锁内校验一致），用于对
      (设备, 费率版本) 建外键，保证引用的费率版本属于班次所属设备。
    """

    __tablename__ = "loss_accountings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shift_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 合计金额（分）；逐段四舍五入后求和，待定价分钟不计入
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    priced_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    unpriced_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    # 按原因汇总：{"maintenance": {"priced_minutes", "amount_cents",
    #                              "unpriced_minutes"}, ...}
    by_reason: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["shift_id"], ["shifts.id"], name="fk_loss_shift"
        ),
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
            "shift_id", "revision", "rate_version", name="uq_loss_accounting"
        ),
        CheckConstraint(
            "total_cents >= 0 AND priced_minutes >= 0 AND unpriced_minutes >= 0",
            name="ck_loss_totals_non_negative",
        ),
    )


class LossAccountingPiece(Base):
    """停机损失账的切分明细：账头同事务固化，之后只读不改。

    每行是一个整分钟半开区间，按 piece_no（时间顺序）排列、互不重叠：
    - priced=TRUE：命中费率的切分段，携带费率（微元/分）与本段单独按分
      四舍五入后的金额（分），seg_no 指回固化归因段；
    - priced=FALSE：未匹配任何费率的待定价分钟（有分钟、金额与费率为空），
      相邻同原因分钟已合并为最大连续区间。
    """

    __tablename__ = "loss_accounting_pieces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    accounting_id: Mapped[int] = mapped_column(
        ForeignKey("loss_accountings.id"), nullable=False, index=True
    )
    piece_no: Mapped[int] = mapped_column(Integer, nullable=False)
    seg_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    priced: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # 仅 priced=TRUE 有值：每分钟费率（微元）与段金额（分，已逐段取整）
    rate_per_minute: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    amount_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "accounting_id", "piece_no", name="uq_loss_accounting_piece"
        ),
        CheckConstraint("end_ts > start_ts", name="ck_loss_pieces_valid_range"),
        CheckConstraint(
            "reason IN ('maintenance','failure','pending')",
            name="ck_loss_pieces_reason",
        ),
        CheckConstraint("minutes > 0", name="ck_loss_pieces_minutes"),
        CheckConstraint(
            "(priced AND rate_per_minute IS NOT NULL AND amount_cents IS NOT NULL "
            "AND seg_no IS NOT NULL) OR "
            "(NOT priced AND rate_per_minute IS NULL AND amount_cents IS NULL)",
            name="ck_loss_pieces_priced_shape",
        ),
    )
