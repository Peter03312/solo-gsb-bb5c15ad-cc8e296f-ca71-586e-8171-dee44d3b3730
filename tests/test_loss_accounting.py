"""停机损失核算算法单元测试（仅依赖标准库）。

覆盖：费率边界切分、定点逐段计费与按分四舍五入（ROUND_HALF_UP）、
合计=逐段四舍五入之和、相邻同质段合并、未匹配费率分钟单列待定价（非零金额）、
费率只匹配同原因归因段、不同原因费率区间可重叠而同原因不可、
行序号按 (起点,终点,原因) 稳定编号、端点相接不算重叠。

可用 pytest 运行，也可直接：python3 tests/test_loss_accounting.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.loss_accounting import (  # noqa: E402
    AttributionSegmentView,
    RateEntry,
    account_loss,
    find_overlaps,
    quantize_money,
)

UTC = timezone.utc
BASE = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def t(minutes: float = 0) -> datetime:
    return BASE + timedelta(minutes=minutes)


def seg(a, b, reason):
    return AttributionSegmentView(start=t(a), end=t(b), reason=reason)


def rate(reason, a, b, amount):
    return RateEntry(
        reason=reason, start=t(a), end=t(b), amount_per_minute=Decimal(amount)
    )


def D(x):
    return Decimal(x)


# ---------------------------------------------------------------- 定点四舍五入


def test_quantize_half_up():
    assert quantize_money(D("0.005")) == D("0.01")  # 半分进位（不是银行家舍入到 0.00）
    assert quantize_money(D("0.015")) == D("0.02")
    assert quantize_money(D("0.014")) == D("0.01")
    assert quantize_money(D("0.124")) == D("0.12")
    assert quantize_money(D("0.125")) == D("0.13")
    assert quantize_money(D("123.456")) == D("123.46")
    assert quantize_money(D("0")) == D("0.00")


# ---------------------------------------------------------------- 基本切分与汇总


def test_single_segment_single_rate():
    # 故障段 0-60 分钟；费率 0.10/分钟覆盖全程
    r = account_loss([seg(0, 60, "failure")], [rate("failure", 0, 120, "0.10")])
    assert len(r.lines) == 1
    ln = r.lines[0]
    assert (ln.start, ln.end) == (t(0), t(60))
    assert ln.priced and ln.reason == "failure"
    assert ln.minutes == 60
    assert ln.amount == D("6.00")
    assert ln.amount_per_minute == D("0.10")
    assert r.total_amount == D("6.00")
    assert r.reason_amounts["failure"] == D("6.00")
    assert r.priced_minutes == 60
    assert r.unpriced_minutes == 0
    assert r.unpriced_intervals == []


def test_split_at_rate_boundary_two_prices():
    # 保养段 0-90，费率在 60 分钟处变价：0-60 @1.00，60-120 @2.00
    r = account_loss(
        [seg(0, 90, "maintenance")],
        [rate("maintenance", 0, 60, "1.00"), rate("maintenance", 60, 120, "2.00")],
    )
    assert [(l.start, l.end) for l in r.lines] == [(t(0), t(60)), (t(60), t(90))]
    assert [l.amount for l in r.lines] == [D("60.00"), D("60.00")]
    assert r.total_amount == D("120.00")
    assert r.priced_minutes == 90
    # 行序号按 (起点,终点,原因)：0-60 为 1，60-120 为 2
    assert [l.rate_entry_no for l in r.lines] == [1, 2]
    assert r.lines[0].rate_start == t(0) and r.lines[0].rate_end == t(60)


def test_rate_partially_overlapping_segment_is_clipped():
    # 故障段 30-90；费率 0-60 @1（覆盖 30-60），费率 60-90 @0.5（覆盖 60-90）
    r = account_loss(
        [seg(30, 90, "failure")],
        [rate("failure", 0, 60, "1.00"), rate("failure", 60, 90, "0.5")],
    )
    assert [(l.minutes, l.amount) for l in r.lines] == [(30, D("30.00")), (30, D("15.00"))]
    assert r.total_amount == D("45.00")


# ---------------------------------------------------------------- 逐段四舍五入后汇总


def test_amount_rounded_per_piece_then_summed():
    # 每分钟 0.005：60 分钟整体 = 0.30；
    # 拆成 3 段（20 分钟各 0.10）与 1 段整体的差异验证"先逐段取整再汇总"
    r = account_loss(
        [seg(0, 20, "failure"), seg(20, 40, "failure"), seg(40, 60, "failure")],
        [rate("failure", 0, 60, "0.005")],
    )
    # 同一费率行相邻 → 合并为一行，金额 0.30
    assert len(r.lines) == 1
    assert r.lines[0].amount == D("0.30")
    assert r.total_amount == D("0.30")

    # 费率边界把它拆成 0.005 与 0.004 两段（费率不同不合并）：
    # 30*0.005=0.15, 30*0.004=0.12 → 0.27
    r2 = account_loss(
        [seg(0, 60, "failure")],
        [rate("failure", 0, 30, "0.005"), rate("failure", 30, 60, "0.004")],
    )
    assert [l.amount for l in r2.lines] == [D("0.15"), D("0.12")]
    assert r2.total_amount == D("0.27")


def test_half_cent_rounding_boundary_per_piece():
    # 3 分钟 @0.005 → 每段（每分钟费率边界切分）各 0.005 → 0.01，合计 0.03
    # 而非整体 0.015 → 0.02
    r = account_loss(
        [seg(0, 3, "failure")],
        [
            rate("failure", 0, 1, "0.005"),
            rate("failure", 1, 2, "0.005"),
            rate("failure", 2, 3, "0.005"),
        ],
    )
    # 三行费率区间端点相接但各自是独立费率行（行号不同）→ 不合并
    assert len(r.lines) == 3
    assert [l.amount for l in r.lines] == [D("0.01"), D("0.01"), D("0.01")]
    assert r.total_amount == D("0.03")


# ---------------------------------------------------------------- 待定价


def test_unmatched_minutes_are_unpriced_not_zero():
    # 保养段 0-60；费率只覆盖 20-40
    r = account_loss([seg(0, 60, "maintenance")], [rate("maintenance", 20, 40, "1.00")])
    assert [(l.start, l.end, l.priced) for l in r.lines] == [
        (t(0), t(20), False),
        (t(20), t(40), True),
        (t(40), t(60), False),
    ]
    unpriced = [l for l in r.lines if not l.priced]
    assert all(l.amount == D("0.00") for l in unpriced)  # 存储 0.00 但语义是未定价
    assert all(l.amount_per_minute is None and l.rate_entry_no is None for l in unpriced)
    priced = r.lines[1]
    assert priced.amount == D("20.00")
    assert r.total_amount == D("20.00")  # 合计不含待定价
    assert r.priced_minutes == 20
    assert r.unpriced_minutes == 40
    assert r.unpriced_intervals == [(t(0), t(20)), (t(40), t(60))]


def test_entire_segment_unpriced():
    r = account_loss([seg(0, 30, "pending")], [])
    assert len(r.lines) == 1
    ln = r.lines[0]
    assert not ln.priced and ln.minutes == 30
    assert r.total_amount == D("0.00")
    assert r.unpriced_minutes == 30
    assert r.unpriced_intervals == [(t(0), t(30))]
    assert r.unpriced_reason_minutes["pending"] == 30


def test_rate_reason_does_not_cross_apply():
    # 故障段 0-30；只有保养费率（即使时间覆盖）也不适用 → 全部待定价
    r = account_loss([seg(0, 30, "failure")], [rate("maintenance", 0, 60, "9.99")])
    assert all(not l.priced for l in r.lines)
    assert r.unpriced_minutes == 30
    assert r.total_amount == D("0.00")


def test_overlapping_rates_of_different_reasons_allowed():
    # 保养段 0-30 + 故障段 20-50（注意：真实固化段不会重叠原因，这里分别计费）
    r = account_loss(
        [seg(0, 30, "maintenance"), seg(20, 50, "failure")],
        [
            rate("maintenance", 0, 60, "1.00"),
            rate("failure", 0, 60, "0.50"),
        ],
    )
    assert r.reason_amounts["maintenance"] == D("30.00")
    assert r.reason_amounts["failure"] == D("15.00")
    assert r.total_amount == D("45.00")
    assert r.unpriced_minutes == 0


# ---------------------------------------------------------------- 合并


def test_adjacent_pieces_with_same_rate_entry_merge_across_segments():
    # 两个端点相接的故障段，同一费率行 → 合并成一行
    r = account_loss(
        [seg(0, 30, "failure"), seg(30, 60, "failure")],
        [rate("failure", 0, 60, "2.00")],
    )
    assert len(r.lines) == 1
    assert (r.lines[0].start, r.lines[0].end) == (t(0), t(60))
    assert r.lines[0].minutes == 60 and r.lines[0].amount == D("120.00")


def test_adjacent_unpriced_pieces_merge_but_not_across_rate_change():
    # 0-20 待定价，20-40 定价，40-60 待定价：两侧待定价不跨定价段合并
    r = account_loss([seg(0, 60, "failure")], [rate("failure", 20, 40, "1.00")])
    assert len(r.lines) == 3
    assert not r.lines[0].priced and r.lines[1].priced and not r.lines[2].priced


def test_lines_sorted_by_start():
    r = account_loss(
        [seg(60, 90, "failure"), seg(0, 30, "failure")],  # 乱序输入
        [rate("failure", 0, 120, "1.00")],
    )
    starts = [l.start for l in r.lines]
    assert starts == sorted(starts)


# ---------------------------------------------------------------- 行序号


def test_entry_numbers_ordered_by_start_end_reason():
    # 乱序给出费率行；编号应按 (起点, 终点, 原因)
    entries = [
        rate("failure", 60, 90, "1"),  # 3rd by start
        rate("failure", 0, 30, "1"),  # 1st
        rate("maintenance", 0, 30, "1"),  # same start/end, maintenance sorts later? reason asc
    ]
    r = account_loss(
        [seg(0, 30, "failure"), seg(60, 90, "failure"), seg(0, 30, "maintenance")],
        entries,
    )
    by_interval = {(l.start, l.end, l.reason): l.rate_entry_no for l in r.lines}
    assert by_interval[(t(0), t(30), "failure")] == 1
    assert by_interval[(t(0), t(30), "maintenance")] == 2
    assert by_interval[(t(60), t(90), "failure")] == 3


# ---------------------------------------------------------------- 发布前重叠校验


def test_find_overlaps_same_reason_half_open():
    # 端点相接不重叠
    entries = [rate("failure", 0, 30, "1"), rate("failure", 30, 60, "1")]
    assert find_overlaps(entries) == []
    # 一分钟重叠
    entries = [rate("failure", 0, 31, "1"), rate("failure", 30, 60, "1")]
    assert len(find_overlaps(entries)) == 1
    # 包含关系也重叠
    entries = [rate("failure", 0, 60, "1"), rate("failure", 10, 20, "1")]
    assert len(find_overlaps(entries)) == 1
    # 不同原因重叠允许
    entries = [rate("failure", 0, 60, "1"), rate("maintenance", 0, 60, "1")]
    assert find_overlaps(entries) == []


def test_non_negative_only_zero_rate_ok():
    r = account_loss([seg(0, 10, "failure")], [rate("failure", 0, 10, "0")])
    assert r.lines[0].priced
    assert r.lines[0].amount == D("0.00")
    assert r.unpriced_minutes == 0  # 零费率是已定价，不是待定价
    assert r.priced_minutes == 10


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
