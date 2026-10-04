"""现场-系统停机对账算法单元测试（仅依赖标准库）。

可用 pytest 运行，也可直接：python3 tests/test_reconciliation.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.attribution import intersect  # noqa: E402
from app.reconciliation import ShiftCoverage, reconcile  # noqa: E402

UTC = timezone.utc
BASE = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def t(hours: float = 0, minutes: float = 0) -> datetime:
    return BASE + timedelta(hours=hours, minutes=minutes)


def minutes(a: float, b: float):
    return (BASE + timedelta(minutes=a), BASE + timedelta(minutes=b))


def cov(
    shift_id: int,
    revision: int,
    seq: int,
    coverage,
    intervals,
) -> ShiftCoverage:
    return ShiftCoverage(
        shift_id=shift_id,
        revision=revision,
        seq=seq,
        coverage=coverage,
        intervals=list(intervals),
    )


# ---------------------------------------------------------------- 交集原语


def test_intersect_half_open():
    assert intersect((t(1), t(3)), (t(2), t(4))) == (t(2), t(3))
    assert intersect((t(1), t(2)), (t(2), t(3))) is None  # 端点相接不算相交
    assert intersect((t(2), t(3)), (t(0), t(5))) == (t(2), t(3))
    assert intersect((t(0), t(1)), (t(2), t(3))) is None


# ---------------------------------------------------------------- 对账


def test_field_and_system_identical_no_diff():
    window = (t(0), t(24))
    shifts = [cov(1, 1, 1, (t(8), t(16)), [(t(10), t(11))])]
    r = reconcile(window[0], window[1], [(t(10), t(11))], shifts)
    assert r.field_only == []
    assert r.system_only == []
    assert r.unreconciled == []
    assert r.field_only_minutes == r.system_only_minutes == r.unreconciled_minutes == 0
    assert r.settled_minutes == 480


def test_field_only_field_extra_downtime():
    window = (t(0), t(24))
    shifts = [cov(1, 1, 1, (t(8), t(16)), [(t(10), t(11))])]
    # 现场 10:00-12:00，系统 10:00-11:00 → 差异 11:00-12:00（60 分钟）
    r = reconcile(window[0], window[1], [(t(10), t(12))], shifts)
    assert r.field_only == [(t(11), t(12))]
    assert r.field_only_minutes == 60
    assert r.system_only == []
    assert r.system_only_minutes == 0
    assert r.unreconciled == []


def test_system_only_missing_field_record():
    window = (t(0), t(24))
    shifts = [cov(1, 1, 1, (t(8), t(16)), [(t(10), t(12))])]
    # 现场仅 10:00-11:00，系统 10:00-12:00 → 11:00-12:00 系统有现场无
    r = reconcile(window[0], window[1], [(t(10), t(11))], shifts)
    assert r.field_only == []
    assert r.system_only == [(t(11), t(12))]
    assert r.system_only_minutes == 60
    assert r.unreconciled == []


def test_both_diffs_at_different_times():
    window = (t(0), t(24))
    shifts = [cov(1, 1, 1, (t(8), t(16)), [(t(10), t(11))])]
    # 现场 09:00-09:30 + 10:00-11:00；系统 10:00-11:00 + 11:30-12:00
    r = reconcile(
        window[0],
        window[1],
        [minutes(9 * 60, 9 * 60 + 30), (t(10), t(11))],
        [cov(1, 1, 1, (t(8), t(16)), [(t(10), t(11)), (t(11, 30), t(12))])],
    )
    assert r.field_only == [minutes(9 * 60, 9 * 60 + 30)]
    assert r.field_only_minutes == 30
    assert r.system_only == [(t(11, 30), t(12))]
    assert r.system_only_minutes == 30
    assert r.unreconciled == []


def test_field_outside_coverage_is_unreconciled_not_diff():
    window = (t(0), t(24))
    # 班次 08:00-16:00，系统停机 10:00-11:00；现场 20:00-21:00（无班次覆盖）
    shifts = [cov(1, 1, 1, (t(8), t(16)), [(t(10), t(11))])]
    r = reconcile(window[0], window[1], [(t(20), t(21))], shifts)
    assert r.field_only == []  # 不算差异
    assert r.system_only == [(t(10), t(11))]
    assert r.system_only_minutes == 60
    assert r.unreconciled == [(t(20), t(21))]
    assert r.unreconciled_minutes == 60
    assert r.settled_minutes == 480


def test_field_interval_spanning_coverage_boundary():
    window = (t(0), t(24))
    # 班次 10:00-11:00；现场 10:30-12:00 → 覆盖内 10:30-11:00 参与比较，
    # 11:00-12:00 未对账；系统 10:00-11:00 全程停机
    shifts = [cov(1, 1, 1, (t(10), t(11)), [(t(10), t(11))])]
    r = reconcile(window[0], window[1], [(t(10, 30), t(12))], shifts)
    assert r.field_only == []
    assert r.system_only == [(t(10), t(10, 30))]
    assert r.system_only_minutes == 30
    assert r.unreconciled == [(t(11), t(12))]
    assert r.unreconciled_minutes == 60


def test_unsettled_gap_between_shifts_is_unreconciled():
    window = (t(0), t(24))
    # 两个不相邻班次 08:00-10:00 与 12:00-14:00；现场 09:00-13:00；两班均无系统停机
    shifts = [
        cov(1, 1, 1, (t(8), t(10)), []),
        cov(2, 1, 2, (t(12), t(14)), []),
    ]
    r = reconcile(window[0], window[1], [(t(9), t(13))], shifts)
    # 覆盖内（09-10、12-13）现场有系统无 → field_only
    assert r.field_only == [(t(9), t(10)), (t(12), t(13))]
    assert r.field_only_minutes == 120
    # 班次间隙 10:00-12:00 → 未对账
    assert r.unreconciled == [(t(10), t(12))]
    assert r.unreconciled_minutes == 120
    assert r.system_only == []
    assert r.settled_minutes == 240


def test_field_intervals_clipped_and_merged_to_window():
    window = (t(8), t(16))
    shifts = [cov(1, 1, 1, (t(8), t(16)), [])]
    # 超出窗口两端且给两段重叠区间：裁剪为 08:00-09:00、08:30-16:00 后合并为整窗
    r = reconcile(
        window[0],
        window[1],
        [(t(7), t(9)), (t(8, 30), t(17))],
        shifts,
    )
    assert r.field_only == [(t(8), t(16))]
    assert r.field_only_minutes == 480
    assert r.unreconciled == []  # 裁剪后全在覆盖（窗口=班次）内


def test_system_segments_clipped_to_window_and_unioned():
    window = (t(9), t(12))
    # 班次 08:00-16:00，系统两段 08:30-09:30 与 11:00-13:00，
    # 裁剪到窗口为 09:00-09:30、11:00-12:00；现场无记录 → 均 system_only
    shifts = [
        cov(1, 1, 1, (t(8), t(16)), [(t(8, 30), t(9, 30)), (t(11), t(13))])
    ]
    r = reconcile(window[0], window[1], [], shifts)
    assert r.system_only == [minutes(9 * 60, 9 * 60 + 30), (t(11), t(12))]
    assert r.system_only_minutes == 90
    assert r.settled_minutes == 180  # 窗口整段被班次覆盖


def test_adjacent_shifts_merge_into_one_coverage():
    window = (t(8), t(16))
    # 端点相接的两个班次，现场 09:00-15:00 无系统停机 → field_only 为整段
    shifts = [
        cov(1, 1, 1, (t(8), t(12)), []),
        cov(2, 1, 2, (t(12), t(16)), []),
    ]
    r = reconcile(window[0], window[1], [(t(9), t(15))], shifts)
    assert r.field_only == [(t(9), t(15))]
    assert r.field_only_minutes == 360
    assert r.unreconciled == []
    assert r.settled_minutes == 480


def test_maximal_contiguous_runs_are_returned():
    window = (t(0), t(24))
    shifts = [cov(1, 1, 1, (t(8), t(16)), [(t(10), t(10, 30)), (t(11), t(11, 30))])]
    # 现场与系统在两段上互换：现场 10:15-10:30（与系统部分重叠）+ 11:30-12:00
    r = reconcile(
        window[0],
        window[1],
        [minutes(10 * 60 + 15, 10 * 60 + 30), minutes(11 * 60 + 30, 12 * 60)],
        shifts,
    )
    # field_only: 11:30-12:00（现场多出，整段）
    assert r.field_only == [(t(11, 30), t(12))]
    assert r.field_only_minutes == 30
    # system_only: 10:00-10:15（系统多出）+ 11:00-11:30
    assert r.system_only == [minutes(10 * 60, 10 * 60 + 15), (t(11), t(11, 30))]
    assert r.system_only_minutes == 45


def test_empty_inputs():
    window = (t(8), t(16))
    r = reconcile(window[0], window[1], [], [])
    assert r.field_only == []
    assert r.system_only == []
    assert r.unreconciled == []
    assert r.settled_minutes == 0
    for name in (
        "field_only_minutes",
        "system_only_minutes",
        "unreconciled_minutes",
    ):
        assert getattr(r, name) == 0


def test_results_sorted_by_time():
    window = (t(0), t(24))
    shifts = [
        cov(1, 1, 1, (t(8), t(16)),
            [(t(9), t(9, 30)), (t(14), t(14, 30)), (t(11), t(11, 15))])
    ]
    r = reconcile(window[0], window[1], [], shifts)
    starts = [iv[0] for iv in r.system_only]
    assert starts == sorted(starts)
    assert starts == [t(9), t(11), t(14)]


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
