"""Tests for the integrity invariants I1-I7 (SPEC.md §5).

An integrity failure is a *data* problem, almost always a pull window that is
too narrow. It must never be counted as a merchant exception, and it must never
be silently dropped: the run reports it on its own channel and states the
exclusion. These tests pin both halves of that.
"""

from __future__ import annotations

from datetime import date

from rlc import invariants
from rlc.calendar_utils import ist_unix
from rlc.entities import Payment, ReconRow, Refund, Settlement
from rlc.loader import Sources

DAY = date(2026, 8, 10)


def _ts(d: date = DAY, hour: int = 12) -> int:
    return ist_unix(d, hour)


def _sources(payments=(), refunds=(), recon=(), settlements=(), disputes=(), ledger=()):
    return Sources.build(
        payments=list(payments),
        refunds=list(refunds),
        disputes=list(disputes),
        settlements=list(settlements),
        recon=list(recon),
        returns_ledger=list(ledger),
    )


def _payment(pid="pay_1", amount=100_000, **kw):
    kw.setdefault("created_at", _ts(date(2026, 8, 1)))
    return Payment(id=pid, amount=amount, **kw)


def _refund(rid="rfnd_1", pid="pay_1", amount=40_000, **kw):
    kw.setdefault("created_at", _ts())
    return Refund(id=rid, payment_id=pid, amount=amount, **kw)


def _refund_row(rid="rfnd_1", pid="pay_1", amount=40_000, **kw):
    kw.setdefault("settled_at", _ts(date(2026, 8, 12), 6))
    kw.setdefault("settlement_id", "setl_1")
    return ReconRow(
        entity_id=rid,
        type="refund",
        debit=kw.pop("debit", amount),
        credit=kw.pop("credit", 0),
        amount=amount,
        payment_id=pid,
        **kw,
    )


# ------------------------------------------------------------ the real data


def test_the_generated_pull_is_clean(sources, cfg):
    """Seeded rejections aside, the generator must not leak integrity failures."""
    report = invariants.run(sources, cfg)
    assert report.channel_counts == {channel: 0 for channel in invariants.DATA_ERROR_CHANNELS}
    assert report.rejection_counts == {
        "NEGATIVE_AMOUNT": 1,
        "NO_PARENT_PAYMENT": 1,
        "REFUND_BEFORE_PAYMENT": 1,
    }


def test_rejections_are_not_exceptions(sources, cfg):
    """Vocabulary check: an integrity failure never becomes an exception code."""
    report = invariants.run(sources, cfg)
    for reasons in report.rejections.values():
        assert reasons
        assert not any(reason.endswith("_OVERDUE") for reason in reasons)


# ----------------------------------------------------------------- I1 and I7


def test_a_refund_without_a_reachable_parent_is_rejected(cfg):
    """NO_PARENT_PAYMENT, never "orphan" — the parent is missing, that is all."""
    report = invariants.run(_sources(refunds=[_refund(pid="pay_missing")]), cfg)
    assert report.reasons_for("rfnd_1") == ["NO_PARENT_PAYMENT"]


def test_a_refund_with_no_payment_id_at_all_is_rejected(cfg):
    report = invariants.run(_sources(refunds=[_refund(pid=None)]), cfg)
    assert report.reasons_for("rfnd_1") == ["NO_PARENT_PAYMENT"]


def test_a_refund_predating_its_payment_is_rejected(cfg):
    payment = _payment(created_at=_ts(date(2026, 8, 20)))
    report = invariants.run(_sources(payments=[payment], refunds=[_refund()]), cfg)
    assert "REFUND_BEFORE_PAYMENT" in report.reasons_for("rfnd_1")


def test_a_currency_mismatch_is_rejected(cfg):
    report = invariants.run(
        _sources(payments=[_payment()], refunds=[_refund(currency="USD")]), cfg
    )
    assert "CURRENCY_MISMATCH" in report.reasons_for("rfnd_1")


def test_an_uncaptured_parent_is_rejected(cfg):
    report = invariants.run(
        _sources(payments=[_payment(status="authorized")], refunds=[_refund()]), cfg
    )
    assert "PARENT_NOT_CAPTURED" in report.reasons_for("rfnd_1")


def test_a_timestamp_after_as_of_is_rejected(cfg):
    """I7: the pull cannot contain the future."""
    future = _refund(created_at=ist_unix(cfg.run.as_of, 12) + 5 * 86_400)
    report = invariants.run(_sources(payments=[_payment()], refunds=[future]), cfg)
    assert "TIMESTAMP_OUT_OF_RANGE" in report.reasons_for("rfnd_1")


# ----------------------------------------------------------------------- I2


def test_over_refunding_a_payment_rejects_every_refund_on_it(cfg):
    """Razorpay returns a 400 for this, so seeing it means the data is corrupt."""
    payment = _payment(amount=50_000)
    refunds = [_refund("rfnd_1", amount=40_000), _refund("rfnd_2", amount=30_000)]
    report = invariants.run(_sources(payments=[payment], refunds=refunds), cfg)
    assert "OVER_CAPTURED_TOTAL" in report.reasons_for("rfnd_1")
    assert "OVER_CAPTURED_TOTAL" in report.reasons_for("rfnd_2")


def test_failed_refunds_do_not_count_toward_the_over_capture_test(cfg):
    """A failed refund moved no money, so it cannot over-refund a payment."""
    payment = _payment(amount=50_000, amount_refunded=40_000, refund_status="partial")
    refunds = [
        _refund("rfnd_1", amount=40_000),
        _refund("rfnd_2", amount=30_000, status="failed"),
    ]
    report = invariants.run(_sources(payments=[payment], refunds=refunds), cfg)
    assert report.rejections == {}


# ----------------------------------------------------------------------- I3


def test_amount_refunded_disagreeing_with_the_refunds_is_an_anomaly_not_a_rejection(cfg):
    """The money moved whatever the summary field says, so the refund still runs."""
    payment = _payment(amount=100_000, amount_refunded=0, refund_status=None)
    report = invariants.run(_sources(payments=[payment], refunds=[_refund()]), cfg)
    assert "AMOUNT_REFUNDED_MISMATCH" in report.payment_anomalies["pay_1"]
    assert report.rejections == {}


# ------------------------------------------------------------------ I4, I5


def test_a_recon_row_pointing_at_the_wrong_payment_is_ignored(cfg):
    """I4: an ignored row cannot satisfy the settlement leg."""
    payment = _payment(amount=100_000, amount_refunded=40_000, refund_status="partial")
    row = _refund_row(pid="pay_other")
    report = invariants.run(_sources(payments=[payment], refunds=[_refund()], recon=[row]), cfg)
    assert report.settlement_rows("rfnd_1") == ()
    assert [e.channel for e in report.data_errors] == ["JOIN_CORRUPTION"]


def test_a_corrupt_row_cannot_manufacture_a_double_deduction(cfg):
    """Two rows, one corrupt, must read as one deduction and not as DOUBLE_DEDUCTED."""
    payment = _payment(amount=100_000, amount_refunded=40_000, refund_status="partial")
    rows = [_refund_row(), _refund_row(pid="pay_other")]
    report = invariants.run(_sources(payments=[payment], refunds=[_refund()], recon=rows), cfg)
    assert len(report.settlement_rows("rfnd_1")) == 1


def test_a_refund_row_carrying_a_fee_is_noted_but_kept(cfg):
    """I5: note it, do not interpret it — leakage lives on the payment row."""
    payment = _payment(amount=100_000, amount_refunded=40_000, refund_status="partial")
    row = _refund_row(fee=118, tax=18)
    report = invariants.run(_sources(payments=[payment], refunds=[_refund()], recon=[row]), cfg)
    assert [e.channel for e in report.data_errors] == ["RECON_FEE_NONZERO"]
    assert len(report.settlement_rows("rfnd_1")) == 1


def test_a_recon_row_for_an_unknown_refund_is_reported(cfg):
    report = invariants.run(_sources(recon=[_refund_row(rid="rfnd_elsewhere")]), cfg)
    assert [e.channel for e in report.data_errors] == ["RECON_ROW_UNMATCHED"]


# ----------------------------------------------------------------------- I6


def test_a_settlement_that_does_not_tie_is_reported_at_batch_level(cfg):
    """I6 is a batch fact; it must not be pushed down onto individual refunds."""
    payment = _payment(amount=100_000, amount_refunded=40_000, refund_status="partial")
    row = _refund_row()
    settlement = Settlement(id="setl_1", amount=999, created_at=_ts())
    report = invariants.run(
        _sources(payments=[payment], refunds=[_refund()], recon=[row], settlements=[settlement]),
        cfg,
    )
    breaks = [e for e in report.data_errors if e.channel == "SETTLEMENT_CONTROL_BREAK"]
    assert len(breaks) == 1
    assert breaks[0].entity_id == "setl_1"
    assert report.rejections == {}
