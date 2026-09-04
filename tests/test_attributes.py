"""Tests for leakage, timing and leg evidence (SPEC.md §7, CLAUDE.md §7).

Leakage is where money arithmetic goes wrong quietly: `fee + tax` double-counts
the GST, and per-refund rounding leaves the control total 1-2 paise short on
every payment with partials. Timing is where dates go wrong quietly: computing a
month boundary in UTC moves an early-morning IST refund into the previous month.
Both failure modes are pinned here with numbers, not with descriptions.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from rlc import attributes, engine
from rlc.calendar_utils import ist_unix, to_ist_date
from rlc.entities import Payment, ReconRow, Refund
from rlc.loader import Sources
from rlc.money import fee_breakdown

AS_OF = date(2026, 9, 30)


def _cfg_at(cfg, as_of: date = AS_OF):
    return dataclasses.replace(cfg, run=dataclasses.replace(cfg.run, as_of=as_of))


def _payment(pid="pay_1", amount=10_000_000, created=date(2026, 8, 1), method="card"):
    fee, _fee_ex, tax = fee_breakdown(amount, 200, 1800)
    return Payment(
        id=pid,
        amount=amount,
        fee=fee,
        tax=tax,
        method=method,
        created_at=ist_unix(created, 10),
        amount_refunded=0,
    )


def _refund(rid="rfnd_1", pid="pay_1", amount=10_000_000, created=date(2026, 8, 10), **kw):
    kw.setdefault("arn", "10000000000000")
    created_at = kw.pop("created_at", None)
    return Refund(
        id=rid,
        payment_id=pid,
        amount=amount,
        created_at=created_at if created_at is not None else ist_unix(created, 12),
        **kw,
    )


def _row(rid="rfnd_1", pid="pay_1", amount=10_000_000, settled=date(2026, 8, 12), **kw):
    settled_at = kw.pop("settled_at", None)
    return ReconRow(
        entity_id=rid,
        type="refund",
        debit=kw.pop("debit", amount),
        credit=0,
        amount=amount,
        payment_id=pid,
        settled_at=settled_at if settled_at is not None else ist_unix(settled, 6),
        settlement_id=kw.pop("settlement_id", "setl_1"),
        **kw,
    )


def _run(cfg, payments, refunds, recon, as_of=AS_OF):
    sources = Sources.build(
        payments=list(payments),
        refunds=list(refunds),
        disputes=[],
        settlements=[],
        recon=list(recon),
        returns_ledger=[],
    )
    scoped = _cfg_at(cfg, as_of)
    closed = engine.close(sources, scoped)
    totals = attributes.annotate(closed, sources, scoped)
    return closed.by_id(), totals


# ------------------------------------------------------------------- leakage


def test_the_worked_example_from_the_spec(cfg):
    """₹1,00,000 fully refunded at 2% + 18% GST leaks ₹2,360, of which ₹360 is GST.

    The customer gets the full ₹1,00,000 back. The merchant is out ₹2,360 on a
    sale that earned nothing.
    """
    payment = _payment(amount=10_000_000)
    assert (payment.fee, payment.tax) == (236_000, 36_000)

    verdicts, totals = _run(cfg, [payment], [_refund()], [_row()])
    verdict = verdicts["rfnd_1"]
    assert verdict.leakage_paise == 236_000
    assert verdict.leakage_gst_paise == 36_000
    assert verdict.leakage_mdr_paise == 200_000
    assert totals.leakage.leakage_bps == 236


def test_leakage_is_never_fee_plus_tax(cfg):
    """`fee` on the Payment entity already includes `tax` (CLAUDE.md §3)."""
    payment = _payment(amount=10_000_000)
    verdicts, _ = _run(cfg, [payment], [_refund()], [_row()])
    assert verdicts["rfnd_1"].leakage_paise != payment.fee + payment.tax
    assert verdicts["rfnd_1"].leakage_paise == payment.fee


def test_the_split_always_reconstitutes_the_total(cfg, sources):
    """total == gst + mdr, on every record of the real month."""
    closed = engine.close(sources, cfg)
    totals = attributes.annotate(closed, sources, cfg)
    for verdict in closed.verdicts:
        assert verdict.leakage_paise == verdict.leakage_gst_paise + verdict.leakage_mdr_paise
    assert totals.leakage.total_paise == totals.leakage.gst_paise + totals.leakage.mdr_paise


def test_three_partials_sum_exactly_to_the_parent_fee(cfg):
    """Independent per-refund rounding is the bug this allocation exists to stop."""
    payment = _payment(amount=100_000)  # ₹1,000 -> fee 2360 paise
    thirds = [33_334, 33_333, 33_333]
    refunds = [
        _refund(f"rfnd_{i}", amount=amount, created=date(2026, 8, 10 + i))
        for i, amount in enumerate(thirds)
    ]
    rows = [
        _row(f"rfnd_{i}", amount=amount, settled=date(2026, 8, 12 + i))
        for i, amount in enumerate(thirds)
    ]
    verdicts, totals = _run(cfg, [payment], refunds, rows)
    allocated = sum(verdicts[r.id].leakage_paise for r in refunds)
    assert allocated == payment.fee
    assert sum(verdicts[r.id].leakage_gst_paise for r in refunds) == payment.tax
    assert totals.rounding_residual_paise == 0


def test_naive_rounding_would_miss_the_parent_fee(cfg):
    """The comparison that justifies largest-remainder allocation."""
    payment = _payment(amount=100_000)
    thirds = [33_334, 33_333, 33_333]
    naive = sum((payment.fee * part + payment.amount // 2) // payment.amount for part in thirds)
    assert naive != payment.fee, "pick a split where naive rounding actually drifts"

    refunds = [
        _refund(f"rfnd_{i}", amount=amount, created=date(2026, 8, 10 + i))
        for i, amount in enumerate(thirds)
    ]
    rows = [
        _row(f"rfnd_{i}", amount=amount, settled=date(2026, 8, 12 + i))
        for i, amount in enumerate(thirds)
    ]
    verdicts, _ = _run(cfg, [payment], refunds, rows)
    assert sum(verdicts[r.id].leakage_paise for r in refunds) == payment.fee


def test_a_partial_refund_carries_only_its_share(cfg):
    payment = _payment(amount=10_000_000)
    verdicts, _ = _run(
        cfg,
        [payment],
        [_refund(amount=2_500_000)],
        [_row(amount=2_500_000)],
    )
    assert verdicts["rfnd_1"].leakage_paise == 59_000  # a quarter of 236_000


def test_a_failed_refund_leaks_nothing_and_does_not_dilute_the_others(cfg):
    """Failed refunds move no money, so they are out of ΣR everywhere."""
    payment = _payment(amount=10_000_000)
    live = _refund("rfnd_1", amount=5_000_000)
    dead = _refund("rfnd_2", amount=5_000_000, status="failed", arn=None,
                   created=date(2026, 8, 11))
    verdicts, _ = _run(cfg, [payment], [live, dead], [_row("rfnd_1", amount=5_000_000)])
    assert verdicts["rfnd_2"].leakage_paise == 0
    assert verdicts["rfnd_1"].leakage_paise == 118_000  # half of 236_000, undiluted


def test_a_rejected_refund_gets_no_attributes(cfg):
    """Stage 0 stopped, so there is no fee to pro-rate and no date to measure."""
    verdicts, totals = _run(cfg, [], [_refund(pid="pay_missing")], [])
    verdict = verdicts["rfnd_1"]
    assert verdict.leakage_paise == 0
    assert verdict.timing_flags == []
    assert verdict.settle_lag_wd is None
    assert totals.leakage.records == 0


def test_by_method_sums_to_the_total(cfg, sources):
    closed = engine.close(sources, cfg)
    totals = attributes.annotate(closed, sources, cfg)
    by_method = totals.leakage.by_method
    assert sum(b["leakage_paise"] for b in by_method.values()) == totals.leakage.total_paise
    assert sum(b["records"] for b in by_method.values()) == totals.leakage.records


def test_the_rounding_residual_stays_below_one_paisa_per_payment(cfg, sources):
    """Spec §7.1: the gap against the exact real-valued total is pure rounding."""
    closed = engine.close(sources, cfg)
    totals = attributes.annotate(closed, sources, cfg)
    payments_with_refunds = len(
        {r.payment_id for r in sources.refunds if r.payment_id in sources.payments_by_id}
    )
    assert abs(totals.rounding_residual_paise) < payments_with_refunds


def test_leakage_is_reported_on_matched_records_too(cfg, sources):
    """It is an attribute, not a bucket: a clean refund still costs its fee.

    The exception is a refund that closed because it was superseded — that
    record moved no money, and its replacement carries the fee instead.
    """
    closed = engine.close(sources, cfg)
    attributes.annotate(closed, sources, cfg)
    matched = [
        v
        for v in closed.verdicts
        if v.closure_state == "CLOSED_MATCHED" and sources.refunds_by_id[v.refund_id].status != "failed"
    ]
    assert matched
    assert all(v.leakage_paise > 0 for v in matched)


def test_a_superseded_failure_carries_no_leakage(cfg, sources):
    """It closed, but the money moved through the replacement refund."""
    closed = engine.close(sources, cfg)
    attributes.annotate(closed, sources, cfg)
    superseded = [v for v in closed.verdicts if "FAILED_SUPERSEDED" in v.annotations]
    assert superseded
    assert all(v.leakage_paise == 0 for v in superseded)


def test_the_leakage_rate_lands_on_the_configured_pricing(cfg, sources):
    """2% + 18% GST is 236 bps; a drift here means the allocation base moved."""
    closed = engine.close(sources, cfg)
    totals = attributes.annotate(closed, sources, cfg)
    assert 230 <= totals.leakage.leakage_bps <= 240


# -------------------------------------------------------------------- timing


def test_a_month_boundary_is_measured_in_ist_not_utc(cfg):
    """CLAUDE.md §12, the first entry on the list.

    01:00 IST on 1 September is 19:30 UTC on 31 August. A UTC month test calls
    this refund an August one and flags a cross-period settlement that never
    happened. In IST both dates are September, so there is nothing to flag.
    """
    created_at = ist_unix(date(2026, 9, 1), 1, 0)
    assert to_ist_date(created_at) == date(2026, 9, 1)

    payment = _payment(created=date(2026, 8, 20))
    refund = _refund(created_at=created_at)
    row = _row(settled=date(2026, 9, 3))
    verdicts, totals = _run(cfg, [payment], [refund], [row])
    assert verdicts["rfnd_1"].timing_flags == []
    assert totals.timing.cross_period_count == 0


def test_a_refund_settled_in_the_next_month_is_cross_period(cfg):
    """The "an August refund reduces September's settlement" problem."""
    payment = _payment(created=date(2026, 8, 20))
    refund = _refund(created_at=ist_unix(date(2026, 8, 31), 23, 30))
    verdicts, totals = _run(cfg, [payment], [refund], [_row(settled=date(2026, 9, 2))])
    assert "CROSS_PERIOD" in verdicts["rfnd_1"].timing_flags
    assert totals.timing.cross_period_count == 1
    assert totals.timing.cross_period_paise == refund.amount


def test_the_settle_lag_is_counted_in_working_days(cfg):
    """Sundays and 2nd/4th Saturdays are not working days, so the lag is not calendar days."""
    payment = _payment(created=date(2026, 8, 1))
    refund = _refund(created=date(2026, 8, 7))  # Friday
    verdicts, _ = _run(cfg, [payment], [refund], [_row(settled=date(2026, 8, 11))])
    # 8 Aug is the 2nd Saturday and 9 Aug a Sunday, so only 10 and 11 count.
    assert verdicts["rfnd_1"].settle_lag_wd == 2


@pytest.mark.parametrize("settled, late", [(date(2026, 8, 13), False), (date(2026, 8, 14), True)])
def test_late_vs_threshold_fires_strictly_past_the_threshold(cfg, settled, late):
    payment = _payment(created=date(2026, 8, 1))
    refund = _refund(created=date(2026, 8, 10))
    verdicts, _ = _run(cfg, [payment], [refund], [_row(settled=settled)])
    assert ("LATE_VS_THRESHOLD" in verdicts["rfnd_1"].timing_flags) is late


def test_timing_needs_an_unambiguous_settlement_date(cfg):
    """A double-deducted refund has two settlement dates, so it has no lag."""
    payment = _payment()
    rows = [_row(settlement_id="setl_1"), _row(settled=date(2026, 8, 13), settlement_id="setl_2")]
    verdicts, totals = _run(cfg, [payment], [_refund()], rows)
    assert verdicts["rfnd_1"].settle_lag_wd is None
    assert verdicts["rfnd_1"].timing_flags == []
    assert totals.timing.measured == 0


def test_the_lag_histogram_covers_every_measured_record(cfg, sources):
    closed = engine.close(sources, cfg)
    totals = attributes.annotate(closed, sources, cfg)
    assert sum(totals.timing.lag_histogram.values()) == totals.timing.measured
    measured = [v for v in closed.verdicts if v.settle_lag_wd is not None]
    assert len(measured) == totals.timing.measured


def test_the_engine_agrees_with_the_seeded_timing_flags(sources, cfg, dataset):
    """Timing labels describe the emitted settlement dates, so they must match."""
    closed = engine.close(sources, cfg)
    attributes.annotate(closed, sources, cfg)
    verdicts = closed.by_id()
    wrong = [
        (rid, sorted(verdicts[rid].timing_flags), sorted(expected["expected_timing_flags"]))
        for rid, expected in dataset.ground_truth.items()
        if sorted(verdicts[rid].timing_flags) != sorted(expected["expected_timing_flags"])
    ]
    assert not wrong, wrong[:5]


# --------------------------------------------------------------------- legs


def test_leg_totals_never_claim_four_verified_legs(cfg, sources):
    """3 verified, 1 evidenced — leg 4 is an ARN, not a bank credit (spec §5)."""
    closed = engine.close(sources, cfg)
    legs = attributes.annotate(closed, sources, cfg).legs
    assert legs.initiated == legs.records
    assert legs.settlement_deducted <= legs.gateway_processed
    assert all(v.legs_verified <= 3 for v in closed.verdicts)


# ---------------------------------------------------------- settlement control


def test_every_paisa_of_settlement_difference_is_explained(sources, cfg):
    """Spec §8: the unexplained difference must be zero, or the report understates."""
    closed = engine.close(sources, cfg)
    control = attributes.annotate(closed, sources, cfg).control
    assert control.unexplained_paise == 0
    assert control.difference_paise == (
        control.explained_by_amount_delta_paise
        + control.explained_by_double_deduction_paise
    )


def test_a_settlement_delta_shows_up_in_the_control_total(cfg):
    payment = _payment()
    verdicts, totals = _run(cfg, [payment], [_refund()], [_row(debit=10_000_500)])
    assert verdicts["rfnd_1"].exception_codes == ["SETTLEMENT_AMOUNT_DELTA"]
    assert totals.control.explained_by_amount_delta_paise == 500
    assert totals.control.unexplained_paise == 0


def test_a_double_deduction_shows_up_as_an_extra_debit(cfg):
    payment = _payment()
    rows = [_row(settlement_id="setl_1"), _row(settled=date(2026, 8, 13), settlement_id="setl_2")]
    _verdicts, totals = _run(cfg, [payment], [_refund()], rows)
    assert totals.control.explained_by_double_deduction_paise == 10_000_000
    assert totals.control.unexplained_paise == 0
