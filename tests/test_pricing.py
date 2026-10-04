"""停机损失核算算法单元测试（仅依赖标准库）。

可用 pytest 运行，也可直接：python3 tests/test_pricing.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.pricing import (  # noqa: E402
    AmountError,
    FrozenSegment,
    RateLine,
    RateTableError,
    format_cents,
    format_micro,
    parse_amount,
    price_segments,
    round_micro_to_cents_half_up,
    validate_rate_lines,
)

UTC = timezone.utc
BASE = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def t(hours: float = 0, minutes: float = 0) -> datetime:
    return BASE + timedelta(hours=hours, minutes=minutes)


def seg(no: int, sh, eh, reason: str) -> FrozenSegment:
    if isinstance(sh, (int, float)):
        sh = t(sh)
    if isinstance(eh, (int, float)):
        eh = t(eh)
    return FrozenSegment(seg_no=no, start=sh, end=eh, reason=reason)


def line(reason: str, sh, eh, amount: str) -> RateLine:
    return RateLine(
        reason=reason, start=sh, end=eh, amount_per_minute=parse_amount(amount)
    )


# ---------------------------------------------------------------- 定点金额


def test_parse_amount_decimal_to_micro():
    assert parse_amount("0") == 0
    assert parse_amount("12.5") == 12_500_000
    assert parse_amount("12.500000") == 12_500_000
    assert parse_amount("100") == 100_000_000
    assert parse_amount("0.000001") == 1
    assert parse_amount("12.345678") == 12_345_678


def test_parse_amount_rejects_invalid():
    bad_inputs = [
        "", " ", "-1", "1.2.3", "1e3", "01", "1.0000001",
        "abc", "+2", "1.", ".5",
    ]
    for bad in bad_inputs:
        try:
            parse_amount(bad)
        except AmountError:
            pass
        else:
            raise AssertionError(f"expected AmountError for {bad!r}")


def test_format_micro_canonical():
    assert format_micro(0) == "0"
    assert format_micro(12_500_000) == "12.5"
    assert format_micro(1) == "0.000001"
    assert format_micro(10_000) == "0.01"
    assert format_micro(100_000_000) == "100"


def test_round_half_up_to_cents():
    assert round_micro_to_cents_half_up(0) == 0
    # 0.3 元/分 × 1 分钟 = 30000 微元 = 3 分，无取整
    assert round_micro_to_cents_half_up(30_000) == 3
    # 0.005 元 = 5000 微元 = 0.5 分 → 向上 1 分
    assert round_micro_to_cents_half_up(5_000) == 1
    # 0.004999 元 → 0 分
    assert round_micro_to_cents_half_up(4_999) == 0
    # 1.005 元 = 1005000 微元 → 101 分（0.5 向上）
    assert round_micro_to_cents_half_up(1_005_000) == 101
    # 1.004999 元 → 100 分
    assert round_micro_to_cents_half_up(1_004_999) == 100


def test_format_cents_two_decimals():
    assert format_cents(0) == "0.00"
    assert format_cents(5) == "0.05"
    assert format_cents(15000) == "150.00"


# ---------------------------------------------------------------- 费率表校验


def test_validate_rate_lines_ok_and_touching():
    rows = [
        line("failure", t(0), t(2), "10"),
        line("failure", t(2), t(4), "20"),  # 端点相接不算重叠
        line("maintenance", t(1), t(3), "5"),  # 不同原因可重叠
    ]
    validate_rate_lines(rows)  # 不抛异常即通过


def test_validate_rate_lines_empty_rejected():
    try:
        validate_rate_lines([])
    except RateTableError:
        pass
    else:
        raise AssertionError("empty rate table must be rejected")


def test_validate_rate_lines_overlap_same_reason_rejected():
    rows = [
        line("failure", t(0), t(2), "10"),
        line("failure", t(1), t(3), "20"),
    ]
    try:
        validate_rate_lines(rows)
    except RateTableError:
        pass
    else:
        raise AssertionError("overlapping same-reason intervals must be rejected")


def test_validate_rate_lines_bad_interval_rejected():
    naive = datetime(2026, 10, 1, 0, 0)  # 缺时区
    r = RateLine(reason="failure", start=naive, end=t(1), amount_per_minute=1)
    try:
        validate_rate_lines([r])
    except RateTableError:
        pass
    else:
        raise AssertionError("naive datetime must be rejected")
    # 未对齐整分钟
    bad = RateLine(
        reason="failure",
        start=t(0) + timedelta(seconds=30),
        end=t(1),
        amount_per_minute=1,
    )
    try:
        validate_rate_lines([bad])
    except RateTableError:
        pass
    else:
        raise AssertionError("non-minute-aligned interval must be rejected")
    # 倒置
    try:
        validate_rate_lines([line("failure", t(2), t(1), "1")])
    except RateTableError:
        pass
    else:
        raise AssertionError("reversed interval must be rejected")


# ---------------------------------------------------------------- 核算切分


def test_pricing_single_segment_single_rate():
    # 故障 10:00-12:00（120 分钟），费率 0.5 元/分 → 60.00 元
    result = price_segments(
        [seg(1, t(10), t(12), "failure")],
        [line("failure", t(0), t(24), "0.5")],
    )
    assert len(result.priced) == 1
    p = result.priced[0]
    assert (p.start, p.end, p.minutes, p.reason) == (t(10), t(12), 120, "failure")
    assert p.amount_cents == 6000
    assert result.total_cents == 6000
    assert result.priced_minutes == 120
    assert result.unpriced_minutes == 0
    assert result.unpriced == []
    assert result.by_reason["failure"].amount_cents == 6000


def test_pricing_split_by_rate_boundary_each_piece_rounded():
    # 故障 10:00-11:00（60 分钟）；10:00-10:30 费率 0.3 元/分，
    # 10:30-11:00 费率 0.005 元/分（30×0.005=0.15 元）。
    result = price_segments(
        [seg(1, t(10), t(11), "failure")],
        [
            line("failure", t(10), t(10, 30), "0.3"),
            line("failure", t(10, 30), t(11), "0.005"),
        ],
    )
    assert len(result.priced) == 2
    assert result.priced[0].minutes == 30
    assert result.priced[0].amount_cents == 900  # 9.00
    assert result.priced[1].minutes == 30
    assert result.priced[1].amount_cents == 15  # 0.15
    assert result.total_cents == 915


def test_pricing_half_up_per_piece_not_on_total():
    # 两段各 1 分钟、费率 0.005 元/分：逐段 0.5 分 → 各 1 分，合计 2 分；
    # 若先汇总（0.01 元）再取整也是 1 分 —— 本断言锁定"逐段先取整"。
    result = price_segments(
        [
            seg(1, t(10), t(10, 30), "failure"),  # 会被费率边界切成两段 1 分钟
        ],
        [
            line("failure", t(10), t(10, 1), "0.005"),
            line("failure", t(10, 1), t(10, 2), "0.005"),
        ],
    )
    assert len(result.priced) == 2
    assert [p.amount_cents for p in result.priced] == [1, 1]
    assert result.total_cents == 2


def test_pricing_unmatched_minutes_are_unpriced_not_zero():
    # 故障段 10:00-12:00；费率只覆盖 11:00-12:00（1 元/分）
    result = price_segments(
        [seg(1, t(10), t(12), "failure")],
        [line("failure", t(11), t(12), "1")],
    )
    assert len(result.priced) == 1
    assert result.priced[0].minutes == 60
    assert result.priced[0].amount_cents == 6000
    assert result.total_cents == 6000  # 待定价分钟绝不按 0 计入
    assert result.unpriced_minutes == 60
    assert result.unpriced == [
        UnpricedIntervalLike(t(10), t(11), "failure", 60, 1)
    ]


class UnpricedIntervalLike:
    def __init__(self, start, end, reason, minutes, seg_no):
        self.start, self.end, self.reason = start, end, reason
        self.minutes, self.seg_no = minutes, seg_no

    def __eq__(self, other):
        return (
            other.start == self.start
            and other.end == self.end
            and other.reason == self.reason
            and other.minutes == self.minutes
            and other.seg_no == self.seg_no
        )


def test_pricing_unpriced_hole_inside_segment():
    # 费率覆盖段首尾、中间缺口：缺口待定价
    result = price_segments(
        [seg(1, t(10), t(12), "failure")],
        [
            line("failure", t(10), t(10, 30), "1"),
            line("failure", t(11, 30), t(12), "1"),
        ],
    )
    assert len(result.priced) == 2
    assert result.priced[0].minutes == 30
    assert result.priced[1].minutes == 30
    assert result.total_cents == 6000
    assert result.unpriced_minutes == 60
    assert len(result.unpriced) == 1
    assert (result.unpriced[0].start, result.unpriced[0].end) == (t(10, 30), t(11, 30))


def test_pricing_unpriced_merges_across_adjacent_segments_same_reason():
    # 两个相邻同原因固化段（10-11、11-12），费率只覆盖 10:00-10:30 与 11:30-12:00：
    # 待定价 10:30-11:30 跨段相接 → 合并为一个最大连续区间
    result = price_segments(
        [
            seg(1, t(10), t(11), "failure"),
            seg(2, t(11), t(12), "failure"),
        ],
        [
            line("failure", t(10), t(10, 30), "1"),
            line("failure", t(11, 30), t(12), "1"),
        ],
    )
    assert result.unpriced_minutes == 60
    assert len(result.unpriced) == 1
    assert (result.unpriced[0].start, result.unpriced[0].end) == (t(10, 30), t(11, 30))
    assert result.unpriced[0].seg_no == 1  # 保留首个并入段号


def test_pricing_unpriced_not_merged_across_reasons():
    # 相邻但原因不同的待定价分钟不合并
    result = price_segments(
        [
            seg(1, t(10), t(11), "failure"),
            seg(2, t(11), t(12), "maintenance"),
        ],
        [line("pending", t(0), t(24), "1")],  # 两个原因都没有费率
    )
    assert result.unpriced_minutes == 120
    assert len(result.unpriced) == 2
    assert [u.reason for u in result.unpriced] == ["failure", "maintenance"]


def test_pricing_reasons_scoped_independently():
    # failure 费率区间与 maintenance 段重叠也不影响 maintenance 选取
    result = price_segments(
        [seg(1, t(10), t(11), "maintenance")],
        [
            line("failure", t(10), t(11), "100"),
            line("maintenance", t(10), t(11), "0.1"),
        ],
    )
    assert result.priced[0].rate_per_minute_micro == parse_amount("0.1")
    assert result.priced[0].amount_cents == 600  # 60 × 0.1 = 6.00


def test_pricing_zero_rate_is_priced_not_unpriced():
    # 金额 0 的费率行是合法定价（金额为 0），不是待定价
    result = price_segments(
        [seg(1, t(10), t(11), "failure")],
        [line("failure", t(10), t(11), "0")],
    )
    assert result.unpriced == []
    assert result.priced[0].amount_cents == 0
    assert result.total_cents == 0
    assert result.priced_minutes == 60


def test_pricing_piece_no_and_totals_by_reason():
    result = price_segments(
        [
            seg(1, t(10), t(11), "failure"),
            seg(2, t(11), t(12), "maintenance"),
        ],
        [
            line("failure", t(10), t(10, 30), "1"),
            line("maintenance", t(11), t(12), "2"),
        ],
    )
    assert [p.piece_no for p in result.priced] == [1, 2]
    assert result.by_reason["failure"].priced_minutes == 30
    assert result.by_reason["failure"].unpriced_minutes == 30
    assert result.by_reason["failure"].amount_cents == 3000
    assert result.by_reason["maintenance"].priced_minutes == 60
    assert result.by_reason["maintenance"].amount_cents == 12000
    assert result.by_reason["pending"].priced_minutes == 0
    # 总分钟守恒：定价 + 待定价 = 全部固化停机分钟
    assert result.priced_minutes + result.unpriced_minutes == 120


def test_pricing_rates_outside_segment_fully_unpriced():
    result = price_segments(
        [seg(1, t(10), t(11), "failure")],
        [line("failure", t(12), t(13), "1")],
    )
    assert result.priced == []
    assert result.total_cents == 0
    assert result.unpriced_minutes == 60
    assert len(result.unpriced) == 1


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
