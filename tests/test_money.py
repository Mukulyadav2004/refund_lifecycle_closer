"""Tests for money arithmetic (spec §7.1).

The allocation tests are the important ones: they are what keeps the leakage
control total in the report tying to the parent payment fee.
"""

import pytest

from rlc.money import (
    allocate_largest_remainder,
    bps_of,
    fee_breakdown,
    format_inr,
    pct_paise,
)


# ------------------------------------------------------------------ pct_paise


def test_pct_paise_two_percent_of_one_lakh():
    # ₹1,00,000 = 10_000_000 paise; 2% = ₹2,000 = 200_000 paise
    assert pct_paise(10_000_000, 200) == 200_000


def test_pct_paise_rounds_half_up():
    # 10_000 paise * 1 bp = 1.0 exactly
    assert pct_paise(10_000, 1) == 1
    # 15_000 paise * 1 bp = 1.5 -> 2
    assert pct_paise(15_000, 1) == 2
    # 14_999 paise * 1 bp = 1.4999 -> 1
    assert pct_paise(14_999, 1) == 1


def test_pct_paise_rejects_negative():
    with pytest.raises(ValueError):
        pct_paise(-1, 200)
    with pytest.raises(ValueError):
        pct_paise(100, -200)


# -------------------------------------------------------------- fee_breakdown


def test_fee_breakdown_matches_the_worked_example():
    """₹1,00,000 at 2% + 18% GST -> fee ₹2,360 of which ₹360 is GST."""
    fee, fee_ex, gst = fee_breakdown(10_000_000, 200, 1800)
    assert fee_ex == 200_000      # ₹2,000
    assert gst == 36_000          # ₹360
    assert fee == 236_000         # ₹2,360, GST-inclusive
    assert fee == fee_ex + gst


def test_fee_is_gst_inclusive_so_fee_plus_tax_double_counts():
    """Guard against the single most common error in this project."""
    fee, fee_ex, gst = fee_breakdown(10_000_000, 200, 1800)
    assert fee - gst == fee_ex               # correct MDR portion
    assert fee + gst != fee                  # fee + tax would overstate by the GST
    assert fee + gst == 272_000              # the wrong number, documented here


# ------------------------------------------------- allocate_largest_remainder


def test_full_refund_takes_the_whole_fee():
    assert allocate_largest_remainder(236_000, [10_000_000], 10_000_000) == [236_000]


def test_half_refund_takes_half_the_fee():
    assert allocate_largest_remainder(236_000, [5_000_000], 10_000_000) == [118_000]


def test_partials_covering_the_base_sum_exactly_to_the_fee():
    """Independent rounding would miss by 1-2 paise; largest remainder does not."""
    parts = [33_333, 33_333, 33_334]
    alloc = allocate_largest_remainder(236_000, parts, 100_000)
    assert sum(alloc) == 236_000
    assert alloc == [78_666, 78_666, 78_668]


def test_naive_rounding_would_not_tie():
    """Documents why the largest-remainder step exists at all.

    A ₹1,000 payment carries a 2,360 paise GST-inclusive fee. Split across three
    near-equal partial refunds, independent rounding overshoots the parent fee by
    one paisa, and the leakage control total in the report stops tying.
    """
    total, base = 2_360, 100_000
    parts = [33_334, 33_333, 33_333]
    naive = [round(total * p / base) for p in parts]
    assert sum(naive) == 2_361 != total

    exact = allocate_largest_remainder(total, parts, base)
    assert sum(exact) == total
    assert exact == [787, 787, 786]


def test_partial_coverage_never_exceeds_the_pro_rata_share():
    # Only 60% of the payment is refunded, so at most 60% of the fee leaks.
    alloc = allocate_largest_remainder(236_000, [40_000, 20_000], 100_000)
    assert sum(alloc) <= 236_000
    assert sum(alloc) == (236_000 * 60_000) // 100_000


def test_allocation_is_deterministic_and_order_stable():
    parts = [1, 1, 1]
    first = allocate_largest_remainder(100, parts, 3)
    second = allocate_largest_remainder(100, parts, 3)
    assert first == second
    assert sum(first) == 100


def test_allocation_handles_zero_total_and_empty_parts():
    assert allocate_largest_remainder(0, [5_000], 10_000) == [0]
    assert allocate_largest_remainder(236_000, [], 10_000) == []


def test_allocation_rejects_parts_exceeding_base():
    """Mirrors the ΣR <= captured invariant, which Razorpay enforces with a 400."""
    with pytest.raises(ValueError):
        allocate_largest_remainder(236_000, [60_000, 60_000], 100_000)


def test_allocation_rejects_bad_base():
    with pytest.raises(ValueError):
        allocate_largest_remainder(100, [1], 0)


def test_many_small_partials_still_tie():
    parts = [7] * 13 + [9]          # sums to 100
    alloc = allocate_largest_remainder(236_000, parts, 100)
    assert sum(alloc) == 236_000
    assert len(alloc) == 14


# ------------------------------------------------------------------ display


def test_format_inr_uses_indian_digit_grouping():
    assert format_inr(236_000) == "₹2,360.00"
    assert format_inr(10_000_000) == "₹1,00,000.00"
    assert format_inr(1_000_000_000) == "₹1,00,00,000.00"
    assert format_inr(5) == "₹0.05"
    assert format_inr(-236_000) == "-₹2,360.00"


def test_bps_of_recovers_the_fee_rate():
    """A full refund of a 2% + 18% GST payment leaks ~236 bps."""
    assert bps_of(236_000, 10_000_000) == 236
    assert bps_of(0, 0) == 0
