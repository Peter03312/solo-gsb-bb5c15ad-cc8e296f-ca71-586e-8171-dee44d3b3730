"""停机损失核算：费率表校验、按费率边界切分与定点金额计算（纯标准库）。

费率表规则
- 费率版本按设备独立递增（版本号由服务端在发布事务内分配，1 起连续）；
- 每行 = 原因 + 带时区的整分钟半开生效区间 [start, end) + 非负每分钟金额；
- 同版本同原因的生效区间互不重叠（端点相接允许）；不同原因可重叠——
  任一分钟只可能被归因为一种原因，费率按 (原因, 时间) 选取；
- 发布须原子完成：一个版本的全部行同事务可见，读不到"发布了一半"的版本。

定点金额规则（不使用二进制浮点）
- 金额入参为十进制字符串，最多 6 位小数、非负；内部一律存为「微元」整数
  （1 元 = 10^6 微元），即 BIGINT 可承载的定点数；
- 每个切分后的定价段：段金额(微元) = 每分钟金额(微元) × 段内分钟数；
  段金额按「分」四舍五入（0.5 分向上，ROUND_HALF_UP），得「分」整数；
- 合计 = 各段「分」整数之和（先逐段取整、再汇总，绝不先汇总后取整）。

核算规则
- 输入为某修订**固化**的归因段（与 attribution.attribute_segments 同形态：
  整分钟半开区间 + 原因，按起点排序、互不重叠，只含停机分钟）；
- 用该原因的费率边界切分：命中费率的子段逐段定价；未命中任何费率的分钟
  单列「待定价」——有分钟数但**无金额**，绝不按零金额计入合计；
- 相邻的待定价分钟（含跨相邻固化段、同原因）合并为最大连续区间。

本模块只依赖标准库，可脱离 Web/数据库独立测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .attribution import REASONS

#: 金额定点标度：1 元 = 10^6 微元（费率最多 6 位小数，精确表示）
AMOUNT_SCALE = 6
MICRO_PER_YUAN = 10**AMOUNT_SCALE
#: 1 分 = 10^4 微元
MICRO_PER_CENT = 10**4

_AMOUNT_RE = re.compile(r"^\d+(?:\.\d+)?$")
#: 整数位数上限（10^12 元/分钟）：与 BIGINT 定点存储及「费率 × 分钟数」
#: 的中间积相容，超出视为非法输入而不是让数据库溢出。
MAX_AMOUNT_INTEGER_DIGITS = 12


class AmountError(ValueError):
    """金额字符串非法（空、非数字、负号/指数、多于 6 位小数等）。"""


class RateTableError(ValueError):
    """费率表非法：原回因因非法、区间非整分钟/倒置或同原因区间重叠。"""


def parse_amount(text: str) -> int:
    """十进制金额字符串 → 微元整数。

    接受如 "0"、"12.5"、"12.500000"；拒绝负数、指数、科学计数法、空串及
    超过 6 位小数的值（负数金额在业务上无意义，"非负"即此处强制）。
    """
    if not isinstance(text, str):
        raise AmountError("amount must be a decimal string")
    s = text.strip()
    if not _AMOUNT_RE.fullmatch(s):
        raise AmountError(
            f"invalid amount {text!r}: expected a non-negative decimal string "
            "without exponent, e.g. '0', '12.5', '12.500000'"
        )
    whole, _, frac = s.partition(".")
    if len(whole) > 1 and whole.startswith("0"):
        raise AmountError(f"invalid amount {text!r}: unnecessary leading zeros")
    if len(whole) > MAX_AMOUNT_INTEGER_DIGITS:
        raise AmountError(
            f"invalid amount {text!r}: at most {MAX_AMOUNT_INTEGER_DIGITS} "
            "integer digits are allowed"
        )
    if len(frac) > AMOUNT_SCALE:
        raise AmountError(
            f"invalid amount {text!r}: at most {AMOUNT_SCALE} decimal places are allowed"
        )
    if frac:
        frac6 = (frac + "0" * AMOUNT_SCALE)[:AMOUNT_SCALE]
    else:
        frac6 = "0" * AMOUNT_SCALE
    return int(whole) * MICRO_PER_YUAN + int(frac6)


def format_micro(amount_micro: int) -> str:
    """微元整数 → 规范十进制字符串（去掉尾随 0；0 元输出 "0"）。"""
    if amount_micro == 0:
        return "0"
    whole, frac = divmod(amount_micro, MICRO_PER_YUAN)
    if frac == 0:
        return str(whole)
    return f"{whole}.{frac:0{AMOUNT_SCALE}d}".rstrip("0")


def round_micro_to_cents_half_up(value_micro: int) -> int:
    """微元金额按分四舍五入（ROUND_HALF_UP，0.5 分向上）→ 分整数（非负）。"""
    return (value_micro + MICRO_PER_CENT // 2) // MICRO_PER_CENT


def format_cents(cents: int) -> str:
    """分整数 → 两位小数十进制字符串（如 15000 -> "150.00"）。"""
    return f"{cents // 100}.{cents % 100:02d}"


def _check_minute_interval(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or start.utcoffset() is None:
        raise RateTableError("rate interval start must include a timezone offset")
    if end.tzinfo is None or end.utcoffset() is None:
        raise RateTableError("rate interval end must include a timezone offset")
    if start.second != 0 or start.microsecond != 0:
        raise RateTableError("rate interval start must be aligned to a whole minute")
    if end.second != 0 or end.microsecond != 0:
        raise RateTableError("rate interval end must be aligned to a whole minute")
    if not start < end:
        raise RateTableError("rate interval start must be earlier than end")


@dataclass(frozen=True)
class RateLine:
    """费率表的一行：某原因在 [start, end) 内每分钟的金额（微元）。"""

    reason: str
    start: datetime
    end: datetime
    amount_per_minute: int

    def validate(self) -> None:
        if self.reason not in REASONS:
            raise RateTableError(f"unknown reason: {self.reason!r}")
        _check_minute_interval(self.start, self.end)
        if self.amount_per_minute < 0:
            raise RateTableError("amount_per_minute must be non-negative")


def validate_rate_lines(lines: list[RateLine]) -> None:
    """校验一个费率版本的全部行：逐行合法且同版本同原因区间互不重叠。

    空表拒绝（发布的必须是"完整费率表"，至少含一行）；端点相接不算重叠。
    """
    if not lines:
        raise RateTableError("rate version must contain at least one entry")
    for line in lines:
        line.validate()
    by_reason: dict[str, list[RateLine]] = {}
    for line in lines:
        by_reason.setdefault(line.reason, []).append(line)
    for reason, rows in by_reason.items():
        rows.sort(key=lambda r: (r.start, r.end))
        prev: RateLine | None = None
        for row in rows:
            if prev is not None and row.start < prev.end:
                raise RateTableError(
                    f"overlapping {reason!r} rate intervals in the same version: "
                    f"[{prev.start.isoformat()}, {prev.end.isoformat()}) and "
                    f"[{row.start.isoformat()}, {row.end.isoformat()})"
                )
            prev = row


@dataclass(frozen=True)
class FrozenSegment:
    """修订固化的一个归因段（停机分钟，半开区间，单一原因）。"""

    seg_no: int
    start: datetime
    end: datetime
    reason: str


@dataclass(frozen=True)
class PricedPiece:
    """一个定价切分段：固化归因段与某费率行相交出的整分钟区间。

    amount_cents 为本段单独按分四舍五入后的金额（分），合计为各段之和。
    """

    piece_no: int
    seg_no: int
    start: datetime
    end: datetime
    reason: str
    minutes: int
    rate_per_minute_micro: int
    amount_cents: int


@dataclass(frozen=True)
class UnpricedInterval:
    """未匹配到任何费率的停机分钟（待定价）：有分钟、无金额。"""

    start: datetime
    end: datetime
    reason: str
    minutes: int
    seg_no: int  # 首个并入的固化段号（可能由相邻同原因段合并而来）


@dataclass(frozen=True)
class ReasonTotal:
    reason: str
    priced_minutes: int = 0
    amount_cents: int = 0
    unpriced_minutes: int = 0


@dataclass(frozen=True)
class PricingResult:
    """一次核算的全部结果。

    - priced：按时间顺序的定价切分段（段号 piece_no 从 1 起），段金额已逐段
      四舍五入到分；total_cents = 各段 amount_cents 之和；
    - unpriced：待定价的最大连续区间（相邻同原因分钟合并，跨固化段也合并），
      不计任何金额；
    - by_reason：按原因的定价分钟/金额/待定价分钟汇总（三类原因恒列出）。
    """

    priced: list[PricedPiece] = field(default_factory=list)
    unpriced: list[UnpricedInterval] = field(default_factory=list)
    total_cents: int = 0
    priced_minutes: int = 0
    unpriced_minutes: int = 0
    by_reason: dict[str, ReasonTotal] = field(default_factory=dict)


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds()) // 60


def price_segments(
    segments: list[FrozenSegment], lines: list[RateLine]
) -> PricingResult:
    """用指定费率版本的费率行，为修订固化归因段核算停机损失。

    segments 按起点排序、互不重叠（固化明细的既有约定）；lines 已通过
    validate_rate_lines 校验。同一固化段被该原因费率行的边界切成多个
    定价段与待定价间隙；每个定价段独立取整后再汇总。
    """
    # 假设输入均已对齐整分钟；按原因分组费率行并按起点排序（同原因互不重叠）
    by_reason: dict[str, list[RateLine]] = {r: [] for r in REASONS}
    for line in lines:
        line.validate()
        by_reason[line.reason].append(line)
    for rows in by_reason.values():
        rows.sort(key=lambda r: (r.start, r.end))

    priced: list[PricedPiece] = []
    raw_unpriced: list[UnpricedInterval] = []
    totals = {r: ReasonTotal(reason=r) for r in REASONS}
    total_priced_minutes = 0
    total_unpriced_minutes = 0
    total_cents = 0

    def _add_unpriced(seg_no: int, start: datetime, end: datetime, reason: str) -> None:
        nonlocal total_unpriced_minutes
        minutes = _minutes(start, end)
        raw_unpriced.append(
            UnpricedInterval(
                start=start, end=end, reason=reason, minutes=minutes, seg_no=seg_no
            )
        )
        total_unpriced_minutes += minutes
        rt = totals[reason]
        totals[reason] = ReasonTotal(
            reason=reason,
            priced_minutes=rt.priced_minutes,
            amount_cents=rt.amount_cents,
            unpriced_minutes=rt.unpriced_minutes + minutes,
        )

    piece_no = 0
    for seg in sorted(segments, key=lambda s: (s.start, s.seg_no)):
        if seg.reason not in REASONS:
            raise RateTableError(f"unknown segment reason: {seg.reason!r}")
        cur = seg.start
        matched = False
        for line in by_reason[seg.reason]:
            # 裁剪到固化段；同原因行互不重叠，按起点顺序推进
            lo, hi = max(line.start, seg.start), min(line.end, seg.end)
            if lo >= hi:
                continue
            matched = True
            if lo > cur:  # 前一费率行结束（或段起点）到本费率行起点：待定价
                _add_unpriced(seg.seg_no, cur, lo, seg.reason)
            piece_no += 1
            minutes = _minutes(lo, hi)
            amount_cents = round_micro_to_cents_half_up(
                line.amount_per_minute * minutes
            )
            priced.append(
                PricedPiece(
                    piece_no=piece_no,
                    seg_no=seg.seg_no,
                    start=lo,
                    end=hi,
                    reason=seg.reason,
                    minutes=minutes,
                    rate_per_minute_micro=line.amount_per_minute,
                    amount_cents=amount_cents,
                )
            )
            total_cents += amount_cents
            total_priced_minutes += minutes
            rt = totals[seg.reason]
            totals[seg.reason] = ReasonTotal(
                reason=seg.reason,
                priced_minutes=rt.priced_minutes + minutes,
                amount_cents=rt.amount_cents + amount_cents,
                unpriced_minutes=rt.unpriced_minutes,
            )
            cur = hi
        if not matched or cur < seg.end:
            _add_unpriced(seg.seg_no, cur, seg.end, seg.reason)

    # 相邻（端点相接）且同原因的待定价间隙合并为最大连续区间；
    # 不同原因或中间隔着未停机分钟（不会相接）则不合并。
    unpriced = _merge_unpriced(raw_unpriced)

    return PricingResult(
        priced=priced,
        unpriced=unpriced,
        total_cents=total_cents,
        priced_minutes=total_priced_minutes,
        unpriced_minutes=total_unpriced_minutes,
        by_reason=totals,
    )


def _merge_unpriced(intervals: list[UnpricedInterval]) -> list[UnpricedInterval]:
    merged: list[UnpricedInterval] = []
    for iv in sorted(intervals, key=lambda x: (x.start, x.reason)):
        if (
            merged
            and merged[-1].end == iv.start
            and merged[-1].reason == iv.reason
        ):
            prev = merged[-1]
            merged[-1] = UnpricedInterval(
                start=prev.start,
                end=iv.end,
                reason=prev.reason,
                minutes=prev.minutes + iv.minutes,
                seg_no=prev.seg_no,
            )
        else:
            merged.append(iv)
    return merged
