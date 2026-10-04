"""停机损失核算核心算法（半开区间，纯标准库）。

规则：
- 输入为某修订**同事务固化的归因区间明细**：按起点排序、互不重叠的半开
  [start, end) 段，每段带一个原因（保养/故障/待判）；
- 费率表为一个已发布**完整版本**的全部费率行：每行含原因、带时区的整分钟
  半开生效区间与非负的每分钟金额；同一版本同一原因的生效区间互不重叠
  （由发布事务在应用层与数据库排他约束双保险下保证，本模块按此前提工作）；
- 以费率边界与归因段边界把全部停机分钟切成"费率恒定"的小片段：同一原因的
  费率区间只适用于同原因的归因段；归因段内没有任何同原因费率覆盖的分钟为
  **待定价分钟**，单独成段，绝不当作零金额；
- 每段金额 = 每分钟金额（定点）× 分钟数，再按"分"（两位小数）银行家无关的
  **四舍五入（ROUND_HALF_UP，半分进位）**；合计 = 各段四舍五入后金额之和，
  而不是先求和再取整 —— 保证逐段金额之和恒等于合计；
- 相邻且原因相同、定价状态相同（同一费率行或同为待定价）的片段合并，
  输出按起点排序的最大连续区间。

金额一律使用 decimal.Decimal 定点运算，不经过任何二进制浮点；费率的原始
定点精度（最多 6 位小数）在段明细中原样保留，合计与段金额按分四舍五入。

本模块只依赖标准库，可脱离 Web/数据库独立测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext

from .attribution import REASONS, merge

Interval = tuple[datetime, datetime]

#: 金额最小记账单位：分（元的两位小数）
CENT = Decimal("0.01")
#: 费率（每分钟金额）允许的最大小数位数（数据库 NUMERIC(18,6) 的配套上限）
MAX_RATE_SCALE = 6


def quantize_money(value: Decimal) -> Decimal:
    """把金额按分四舍五入（ROUND_HALF_UP：半分进位）。"""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RateEntry:
    """费率表一个版本中的一行：某原因在 [start, end) 的每分钟金额。"""

    reason: str
    start: datetime
    end: datetime
    amount_per_minute: Decimal


@dataclass(frozen=True)
class AttributionSegmentView:
    """核算输入：修订固化的一段归因区间（半开，带原因）。"""

    start: datetime
    end: datetime
    reason: str


@dataclass(frozen=True)
class LossLine:
    """切分结果的一行：一个原因与费率（或待定价）恒定的最大连续区间。"""

    start: datetime
    end: datetime
    reason: str
    minutes: int
    priced: bool
    amount_per_minute: Decimal | None
    amount: Decimal  # 已按分四舍五入的段金额（待定价段为 0）
    # 本行依据的费率行序号（发布时按 (起点, 终点, 原因) 排序后从 1 开始）；
    # 待定价行为 None
    rate_entry_no: int | None = None
    # 费率行的半开生效区间（回显，便于核对费率边界）；待定价行为 None
    rate_start: datetime | None = None
    rate_end: datetime | None = None


@dataclass(frozen=True)
class LossResult:
    """核算结果：切分明细、合计、各原因小计与待定价分钟（区间合并后）。"""

    lines: list[LossLine] = field(default_factory=list)
    total_amount: Decimal = Decimal("0.00")
    reason_amounts: dict[str, Decimal] = field(default_factory=dict)
    reason_minutes: dict[str, int] = field(default_factory=dict)
    priced_minutes: int = 0
    unpriced_minutes: int = 0
    unpriced_intervals: list[Interval] = field(default_factory=list)
    unpriced_reason_minutes: dict[str, int] = field(default_factory=dict)


def _minutes(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds()) // 60


def find_overlaps(
    entries: list[RateEntry],
) -> list[tuple[int, int]]:
    """检查同原因半开区间重叠，返回重叠行的**下标对**（端点相接不算重叠）。

    不同原因的区间允许重叠（各自只适用于同原因的归因段）。
    """
    overlaps: list[tuple[int, int]] = []
    by_reason: dict[str, list[tuple[int, RateEntry]]] = {}
    for idx, e in enumerate(entries):
        by_reason.setdefault(e.reason, []).append((idx, e))
    for rows in by_reason.values():
        rows.sort(key=lambda x: (x[1].start, x[1].end))
        for i in range(len(rows)):
            ia, ea = rows[i]
            for j in range(i + 1, len(rows)):
                ib, eb = rows[j]
                if eb.start >= ea.end:
                    break  # 已按起点排序，后续区间起点更远，不可能再相交
                if ea.start < eb.end and eb.start < ea.end:
                    overlaps.append((ia, ib))
    return overlaps


def account_loss(
    segments: list[AttributionSegmentView],
    rate_entries: list[RateEntry],
) -> LossResult:
    """用一个已发布完整费率版本对修订固化的归因段核算停机损失。

    - 以归因段端点与同原因费率边界把停机分钟切成费率恒定的小片段；
    - 逐片段以定点数计算 金额 = 每分钟费率 × 分钟数，再按分四舍五入；
    - 没有任何同原因费率覆盖的片段为待定价（priced=False，金额不取零，单独列出）；
    - 相邻且原因、费率行（或同为待定价）相同的片段合并为最大连续行；
    - 合计 = 各已定价行四舍五入后金额之和。
    """
    # 费率行按原因分组并按起点排序（同原因不重叠是本函数的前提）
    rates_by_reason: dict[str, list[RateEntry]] = {r: [] for r in REASONS}
    for e in rate_entries:
        rates_by_reason.setdefault(e.reason, []).append(e)
    for rows in rates_by_reason.values():
        rows.sort(key=lambda e: (e.start, e.end))

    # 归因段也按起点排序（固化段实际已按起点排列），保证相邻同质片段可合并
    segments = sorted(segments, key=lambda s: (s.start, s.end))

    # 费率行号映射：发布响应/存储按 (起点, 终点, 原因) 排序编号；这里按同样
    # 顺序建立 id 身份 -> 行号，保证核算明细中的费率行号与发布一致。
    ordered = sorted(
        enumerate(rate_entries),
        key=lambda x: (x[1].start, x[1].end, x[1].reason),
    )
    no_by_id: dict[int, int] = {id(e): no for no, (_i, e) in enumerate(ordered, start=1)}

    # 先切成"费率恒定"的原子片段，再合并相邻同质片段
    # piece: [start, end, reason, RateEntry|None(None=待定价)]
    pieces: list[list] = []
    unpriced_raw: dict[str, list[Interval]] = {r: [] for r in REASONS}
    for seg in segments:
        s, e, reason = seg.start, seg.end, seg.reason
        points = {s, e}
        for re_ in rates_by_reason.get(reason, []):
            if re_.start < e and re_.end > s:  # 与该归因段相交（半开）的费率边界才切分
                points.add(max(re_.start, s))
                points.add(min(re_.end, e))
        pts = sorted(points)
        for a, b in zip(pts, pts[1:]):
            if a >= b:
                continue
            match: RateEntry | None = None
            for re_ in rates_by_reason.get(reason, []):
                if re_.start <= a and re_.end >= b:
                    match = re_
                    break  # 同原因不重叠，至多一个
            pieces.append([a, b, reason, match])
            if match is None:
                unpriced_raw.setdefault(reason, []).append((a, b))

    lines: list[LossLine] = []
    for a, b, reason, match in pieces:
        mins = _minutes(a, b)
        if match is None:
            amount = Decimal("0.00")
            rate_no = rate_s = rate_e = None
            priced = False
        else:
            with localcontext() as ctx:
                ctx.prec = 28
                amount = quantize_money(match.amount_per_minute * Decimal(mins))
            rate_no = no_by_id[id(match)]
            rate_s, rate_e = match.start, match.end
            priced = True
        # 相邻且原因相同、定价状态相同（同一费率行对象或同为待定价）→ 合并
        if lines and lines[-1].end == a and lines[-1].reason == reason:
            prev = lines[-1]
            same_rate = (
                prev.rate_entry_no == rate_no
                and (prev.priced == priced)
                and (prev.rate_start == rate_s)
                and (prev.rate_end == rate_e)
            )
            if same_rate:
                merged_amount = quantize_money(prev.amount + amount)
                lines[-1] = LossLine(
                    start=prev.start,
                    end=b,
                    reason=reason,
                    minutes=prev.minutes + mins,
                    priced=priced,
                    amount_per_minute=match.amount_per_minute if match is not None else None,
                    amount=merged_amount,
                    rate_entry_no=rate_no,
                    rate_start=rate_s,
                    rate_end=rate_e,
                )
                continue
        lines.append(
            LossLine(
                start=a,
                end=b,
                reason=reason,
                minutes=mins,
                priced=priced,
                amount_per_minute=match.amount_per_minute if match is not None else None,
                amount=amount,
                rate_entry_no=rate_no,
                rate_start=rate_s,
                rate_end=rate_e,
            )
        )

    total = Decimal("0.00")
    reason_amounts: dict[str, Decimal] = {r: Decimal("0.00") for r in REASONS}
    reason_minutes: dict[str, int] = {r: 0 for r in REASONS}
    unpriced_reason_minutes: dict[str, int] = {r: 0 for r in REASONS}
    priced_minutes = 0
    # 合并按归因段顺序进行（归因段输入通常已按起点排序）；最终统一按起点排序，
    # 使输出对输入顺序免疫
    lines.sort(key=lambda ln: (ln.start, ln.end))
    for ln in lines:
        reason_minutes[ln.reason] = reason_minutes.get(ln.reason, 0) + ln.minutes
        if ln.priced:
            total += ln.amount
            reason_amounts[ln.reason] += ln.amount
            priced_minutes += ln.minutes
        else:
            unpriced_reason_minutes[ln.reason] = (
                unpriced_reason_minutes.get(ln.reason, 0) + ln.minutes
            )

    unpriced_intervals: list[Interval] = []
    unpriced_minutes = 0
    for reason, ivs in unpriced_raw.items():
        merged = merge(ivs)
        unpriced_intervals.extend(merged)
        unpriced_minutes += sum(_minutes(a, b) for a, b in merged)
    unpriced_intervals.sort()

    # 合计再 quantize 一次（逐项均已为分，相加不产生新尾数，保持两位小数规范）
    total = quantize_money(total)
    reason_amounts = {r: quantize_money(v) for r, v in reason_amounts.items()}
    # 清掉未出现原因的零值条目保留三类齐全，方便调用方固定输出
    return LossResult(
        lines=lines,
        total_amount=total,
        reason_amounts=reason_amounts,
        reason_minutes=reason_minutes,
        priced_minutes=priced_minutes,
        unpriced_minutes=unpriced_minutes,
        unpriced_intervals=unpriced_intervals,
        unpriced_reason_minutes=unpriced_reason_minutes,
    )
