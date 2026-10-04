"""现场停机记录与系统已结算归因的对账算法（半开区间，纯标准库）。

规则：
- 现场区间先裁剪到请求窗口 [win_start, win_end) 并合并重叠；
- 系统停机为各相交班次在固定变更序号下所选修订的固化归因明细（三类归因段）
  的并集，因此系统停机只可能落在"已结算覆盖"内；
- 已结算覆盖 = 各相交班次区间与窗口的交集的并集（不要求该分钟已停机）；
- 仅在已结算覆盖内逐分钟比较：
  - 现场有、系统无 → field_only（差异）；
  - 系统有、现场无 → system_only（差异）；
- 现场区间落在已结算覆盖之外的分钟为 unreconciled（未对账，不算差异）。

所有输入均须已对齐整分钟；区间一律半开 [start, end)。本模块只依赖标准库，
可脱离 Web/数据库独立测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .attribution import Interval, intersect, merge, subtract, total_minutes


@dataclass(frozen=True)
class ShiftCoverage:
    """一个已结算班次对对账窗口的贡献。

    intervals 为该班次按固定序号选定的修订的固化归因明细（三类归因段），
    调用前应已裁剪到请求窗口；coverage 为班次区间与窗口的交集。
    """

    shift_id: int
    revision: int
    seq: int
    coverage: Interval
    intervals: list[Interval]


@dataclass(frozen=True)
class ReconciliationResult:
    """对账结果：三类最大连续区间（均按时间顺序）及各自分钟总数。"""

    field_only: list[Interval] = field(default_factory=list)
    system_only: list[Interval] = field(default_factory=list)
    unreconciled: list[Interval] = field(default_factory=list)
    field_only_minutes: int = 0
    system_only_minutes: int = 0
    unreconciled_minutes: int = 0
    settled_minutes: int = 0


def reconcile(
    win_start: datetime,
    win_end: datetime,
    field_intervals: list[Interval],
    shifts: list[ShiftCoverage],
) -> ReconciliationResult:
    """在窗口 [win_start, win_end) 内对账现场记录与系统已结算归因。

    现场区间视为原始上报（可超出窗口），本函数先裁剪到窗口再合并；
    shifts 中各班次的 intervals/coverage 同样会被裁剪到窗口后参与运算。
    """
    window = [(win_start, win_end)]

    # 现场：裁剪到窗口并合并重叠（端点相接也合并，保持半开语义不漏分钟）
    field_clipped: list[Interval] = []
    for s, e in field_intervals:
        iv = intersect((s, e), (win_start, win_end))
        if iv is not None:
            field_clipped.append(iv)
    field_union = merge(field_clipped)

    # 系统停机：所选修订固化明细（三类归因段）的并集；已结算覆盖：班次区间并集
    system_raw: list[Interval] = []
    coverage_raw: list[Interval] = []
    for sh in shifts:
        for s, e in sh.intervals:
            iv = intersect((s, e), (win_start, win_end))
            if iv is not None:
                system_raw.append(iv)
        iv = intersect(sh.coverage, (win_start, win_end))
        if iv is not None:
            coverage_raw.append(iv)
    system_union = merge(system_raw)
    coverage_union = merge(coverage_raw)

    # 仅在已结算覆盖内比较：
    #   现场有系统无 = 现场 ∩ 覆盖 − 系统
    #   系统有现场无 = 系统 − 现场（系统段恒在覆盖内）
    field_in_coverage = merge(
        [iv for iv in subtract(field_union, subtract(window, coverage_union))]
    )
    field_only = subtract(field_in_coverage, system_union)
    system_only = subtract(system_union, field_union)

    # 现场分钟不在已结算班次内 → 未对账（不算差异）
    unreconciled = subtract(field_union, coverage_union)

    return ReconciliationResult(
        field_only=field_only,
        system_only=system_only,
        unreconciled=unreconciled,
        field_only_minutes=total_minutes(field_only),
        system_only_minutes=total_minutes(system_only),
        unreconciled_minutes=total_minutes(unreconciled),
        settled_minutes=total_minutes(coverage_union),
    )
