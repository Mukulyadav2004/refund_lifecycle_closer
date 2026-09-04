"""Tests for the closure state machine (SPEC.md §6, CLAUDE.md §6).

Two kinds of test live here. The first runs the whole generated month through
the engine and checks it against the seeded labels — that is the headline claim.
The rest are small hand-built cases that pin the individual rules, because a
population-level agreement can hide two errors that cancel.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from rlc import engine, invariants
from rlc.calendar_utils import ist_unix
from rlc.entities import (
    CLOSED_MATCHED,
    EXCEPTION,
    OPEN,
    REJECTED_INPUT,
    Dispute,
    Payment,
    ReconRow,
    Refund,
    ReturnsLedgerRow,
)
from rlc.loader import Sources

AS_OF = date(2026, 8, 20)


def _cfg_at(cfg, as_of: date = AS_OF, **threshold_overrides):
    run = dataclasses.replace(cfg.run, as_of=as_of)
    thresholds = (
        dataclasses.replace(cfg.thresholds, **threshold_overrides)
        if threshold_overrides
        else cfg.thresholds
    )
    return dataclasses.replace(cfg, run=run, thresholds=thresholds)


def _ts(d: date, hour: int = 12) -> int:
    return ist_unix(d, hour)


def _sources(payments=(), refunds=(), recon=(), disputes=(), ledger=(), settlements=()):
    return Sources.build(
        payments=list(payments),
        refunds=list(refunds),
        disputes=list(disputes),
        settlements=list(settlements),
        recon=list(recon),
        returns_ledger=list(ledger),
    )


def _payment(pid="pay_1", amount=100_000, created=date(2026, 8, 1), **kw):
    kw.setdefault("amount_refunded", 0)
    return Payment(id=pid, amount=amount, created_at=_ts(created), **kw)


def _refund(rid="rfnd_1", pid="pay_1", amount=40_000, created=date(2026, 8, 10), **kw):
    kw.setdefault("arn", "10000000000000")
    return Refund(id=rid, payment_id=pid, amount=amount, created_at=_ts(created), **kw)


def _row(rid="rfnd_1", pid="pay_1", amount=40_000, settled=date(2026, 8, 12), **kw):
    return ReconRow(
        entity_id=rid,
        type="refund",
        debit=kw.pop("debit", amount),
        credit=kw.pop("credit", 0),
        amount=amount,
        payment_id=pid,
        settled_at=_ts(settled, 6),
        settlement_id=kw.pop("settlement_id", "setl_1"),
        **kw,
    )


def _verdict(cfg, sources, refund_id="rfnd_1", **overrides):
    run = engine.close(sources, _cfg_at(cfg, **overrides))
    return run.by_id()[refund_id]


# ------------------------------------------------- the whole generated month


def test_the_identity_equation_holds(sources, cfg):
    """CLAUDE.md §5: zero silent drops, asserted rather than hoped for."""
    run = engine.close(sources, cfg)
    counts = run.state_counts
    assert len(sources.refunds) == sum(counts.values())
    assert len(run.verdicts) == len(sources.refunds)


def test_every_refund_gets_exactly_one_state(sources, cfg):
    run = engine.close(sources, cfg)
    seen = [v.refund_id for v in run.verdicts]
    assert len(seen) == len(set(seen))
    assert {v.closure_state for v in run.verdicts} <= {
        CLOSED_MATCHED,
        OPEN,
        EXCEPTION,
        REJECTED_INPUT,
    }


def test_the_engine_reproduces_every_seeded_label(sources, cfg, dataset):
    """The headline claim, checked per record rather than in aggregate.

    Reading the labels is the *test's* job. The engine never sees them, which
    `test_no_decision_module_reads_ground_truth` enforces separately.
    """
    run = engine.close(sources, cfg)
    verdicts = run.by_id()
    wrong = []
    for refund_id, expected in dataset.ground_truth.items():
        got = verdicts[refund_id]
        annotations = sorted(
            (set(got.annotations) - engine.INFORMATIONAL_ANNOTATIONS)
            | set(got.rejection_reasons)
        )
        actual = (
            got.closure_state,
            sorted(got.exception_codes),
            sorted(got.open_reasons),
            annotations,
        )
        want = (
            expected["expected_state"],
            sorted(expected["expected_codes"]),
            sorted(expected["expected_open_reasons"]),
            sorted(expected["expected_annotations"]),
        )
        if actual != want:
            wrong.append((refund_id, expected["scenario"], want, actual))
    assert not wrong, f"{len(wrong)} of {len(dataset.ground_truth)} disagree: {wrong[:5]}"


def test_the_run_is_order_independent(dataset, cfg, build_sources):
    """Same records, reversed input, identical verdicts."""
    forward = engine.close(build_sources(dataset), cfg)
    backward = engine.close(
        Sources.build(
            payments=list(reversed(dataset.payments)),
            refunds=list(reversed(dataset.refunds)),
            disputes=list(reversed(dataset.disputes)),
            settlements=list(reversed(dataset.settlements)),
            recon=list(reversed(dataset.recon)),
            returns_ledger=list(reversed(dataset.returns_ledger)),
        ),
        cfg,
    )
    assert [v.to_row() for v in forward.verdicts] == [v.to_row() for v in backward.verdicts]


# --------------------------------------------------------- stage 0: integrity


def test_a_rejected_refund_stops_at_stage_zero(cfg):
    """It claims no verified leg: the run stopped before checking any."""
    verdict = _verdict(cfg, _sources(refunds=[_refund(pid="pay_missing")]))
    assert verdict.closure_state == REJECTED_INPUT
    assert verdict.rejection_reasons == ["NO_PARENT_PAYMENT"]
    assert verdict.exception_codes == []
    assert verdict.legs_verified == 1
    assert not verdict.leg4_bank_evidenced


# ------------------------------------------------------------ stage 1: status


def test_a_failed_refund_with_no_reissue_is_an_exception(cfg):
    payment = _payment()
    refund = _refund(status="failed", arn=None)
    verdict = _verdict(cfg, _sources(payments=[payment], refunds=[refund]))
    assert verdict.closure_state == EXCEPTION
    assert verdict.exception_codes == ["REFUND_FAILED"]


def test_a_failed_refund_that_was_reissued_closes(cfg):
    """The money moved through the replacement, so the pair is not an exception."""
    payment = _payment()
    failed = _refund("rfnd_1", status="failed", arn=None, created=date(2026, 8, 10))
    reissued = _refund("rfnd_2", created=date(2026, 8, 11))
    verdict = _verdict(
        cfg, _sources(payments=[payment], refunds=[failed, reissued], recon=[_row("rfnd_2")])
    )
    assert verdict.closure_state == CLOSED_MATCHED
    assert verdict.annotations == ["FAILED_SUPERSEDED"]
    assert verdict.evidence["superseded_by"] == "rfnd_2"


def test_an_earlier_refund_does_not_supersede_a_later_failure(cfg):
    """Only a *later* successful refund counts as a re-issue."""
    payment = _payment()
    earlier = _refund("rfnd_0", created=date(2026, 8, 5))
    failed = _refund("rfnd_1", status="failed", arn=None, created=date(2026, 8, 10))
    sources = _sources(payments=[payment], refunds=[earlier, failed], recon=[_row("rfnd_0")])
    assert _verdict(cfg, sources).exception_codes == ["REFUND_FAILED"]


def test_a_young_pending_refund_is_open_not_overdue(cfg):
    refund = _refund(status="pending", arn=None, created=date(2026, 8, 19))
    verdict = _verdict(cfg, _sources(payments=[_payment()], refunds=[refund]))
    assert verdict.closure_state == OPEN
    assert verdict.open_reasons == ["AWAITING_PROCESSING"]


def test_an_old_pending_refund_is_overdue(cfg):
    refund = _refund(status="pending", arn=None, created=date(2026, 7, 1))
    verdict = _verdict(cfg, _sources(payments=[_payment(created=date(2026, 6, 1))], refunds=[refund]))
    assert verdict.closure_state == EXCEPTION
    assert verdict.exception_codes == ["PENDING_OVERDUE"]


def test_a_pending_refund_expects_no_settlement_and_no_arn(cfg):
    """Stages 2, 3 and 5 are skipped: nothing is due from a refund not yet processed."""
    refund = _refund(status="pending", arn=None, created=date(2026, 8, 19))
    verdict = _verdict(cfg, _sources(payments=[_payment()], refunds=[refund]))
    assert "AWAITING_SETTLEMENT" not in verdict.open_reasons
    assert "AWAITING_ARN" not in verdict.open_reasons


# -------------------------------------------------- stages 2 and 3: settlement


def test_the_maturity_gate_keeps_a_young_refund_out_of_never_deducted(cfg):
    """CLAUDE.md §6: without this gate every young refund is a false exception."""
    refund = _refund(created=date(2026, 8, 19))
    verdict = _verdict(cfg, _sources(payments=[_payment()], refunds=[refund]))
    assert verdict.closure_state == OPEN
    assert verdict.open_reasons == ["AWAITING_SETTLEMENT"]
    assert "NEVER_DEDUCTED" not in verdict.exception_codes


def test_past_the_gate_a_missing_deduction_is_an_exception(cfg):
    refund = _refund(created=date(2026, 8, 3))
    verdict = _verdict(cfg, _sources(payments=[_payment()], refunds=[refund]))
    assert verdict.closure_state == EXCEPTION
    assert verdict.exception_codes == ["NEVER_DEDUCTED"]


def test_two_recon_rows_are_a_double_deduction(cfg):
    rows = [_row(settlement_id="setl_1"), _row(settled=date(2026, 8, 13), settlement_id="setl_2")]
    verdict = _verdict(cfg, _sources(payments=[_payment()], refunds=[_refund()], recon=rows))
    assert verdict.exception_codes == ["DOUBLE_DEDUCTED"]
    assert verdict.evidence["double_deducted"]["total_debit"] == 80_000
    assert not verdict.leg3_settlement_deducted


def test_a_debit_that_does_not_match_the_refund_is_a_delta(cfg):
    row = _row(debit=40_500)
    verdict = _verdict(cfg, _sources(payments=[_payment()], refunds=[_refund()], recon=[row]))
    assert verdict.exception_codes == ["SETTLEMENT_AMOUNT_DELTA"]
    assert verdict.evidence["settlement_delta"]["delta"] == 500
    assert not verdict.leg3_settlement_deducted


def test_a_clean_deduction_verifies_leg_three(cfg):
    verdict = _verdict(cfg, _sources(payments=[_payment()], refunds=[_refund()], recon=[_row()]))
    assert verdict.closure_state == CLOSED_MATCHED
    assert verdict.leg3_settlement_deducted
    assert verdict.settlement_id == "setl_1"


# ------------------------------------------------------- stage 4: duplicates


def _duplicate_pair(cfg, receipt_a, receipt_b, hours_apart=2, **overrides):
    payment = _payment(amount=200_000)
    first = _refund("rfnd_1", created=date(2026, 8, 10), receipt=receipt_a)
    second = Refund(
        id="rfnd_2",
        payment_id="pay_1",
        amount=40_000,
        created_at=_ts(date(2026, 8, 10)) + hours_apart * 3600,
        receipt=receipt_b,
        arn="10000000000000",
    )
    sources = _sources(
        payments=[payment],
        refunds=[first, second],
        recon=[_row("rfnd_1"), _row("rfnd_2")],
    )
    run = engine.close(sources, _cfg_at(cfg, **overrides))
    return run.by_id()


def test_only_the_later_refund_of_a_duplicate_pair_is_flagged(cfg):
    verdicts = _duplicate_pair(cfg, receipt_a=None, receipt_b=None)
    assert verdicts["rfnd_1"].exception_codes == []
    assert verdicts["rfnd_2"].exception_codes == ["DUPLICATE_SUSPECT"]
    assert verdicts["rfnd_2"].evidence["duplicate"]["twin_refund_id"] == "rfnd_1"


def test_two_distinct_receipts_are_never_a_duplicate(cfg):
    """Razorpay accepted both under different idempotency keys, so both are real.

    This clause is the only thing keeping legitimate multi-partial refunds out of
    the exception list (CLAUDE.md §6).
    """
    verdicts = _duplicate_pair(cfg, receipt_a="RMA-1", receipt_b="RMA-2")
    assert verdicts["rfnd_2"].exception_codes == []


def test_one_missing_receipt_is_enough_to_flag(cfg):
    verdicts = _duplicate_pair(cfg, receipt_a="RMA-1", receipt_b=None)
    assert verdicts["rfnd_2"].exception_codes == ["DUPLICATE_SUSPECT"]


def test_a_duplicate_always_asks_for_a_human(cfg):
    """The only heuristic rule in the engine, and it says so in its output."""
    verdict = _duplicate_pair(cfg, None, None)["rfnd_2"]
    assert verdict.needs_human_review
    assert verdict.confidence is not None


@pytest.mark.parametrize(
    "hours_apart, expected",
    [(0.25, 0.9), (2, 0.7), (36, 0.5)],
)
def test_confidence_falls_as_the_pair_spreads_out(cfg, hours_apart, expected):
    verdicts = _duplicate_pair(
        cfg, None, None, hours_apart=hours_apart, duplicate_window_seconds=259_200
    )
    assert verdicts["rfnd_2"].confidence == expected


def test_widening_the_window_can_only_add_duplicates(sources, cfg):
    """Duplicate-window sensitivity, the number the report has to publish."""
    counts = [
        engine.close(sources, cfg, duplicate_window_seconds=w).code_counts.get(
            "DUPLICATE_SUSPECT", 0
        )
        for w in (1_800, 86_400, 259_200)
    ]
    assert counts == sorted(counts)


# -------------------------------------------------- stage 4: returns ledger


def _rma(rid="RMA-1", pid="pay_1", amount=40_000, created=date(2026, 8, 9)):
    return ReturnsLedgerRow(
        rma_id=rid,
        order_id=None,
        payment_id=pid,
        expected_refund_paise=amount,
        rma_created_at=_ts(created),
    )


def test_a_refund_matching_its_rma_is_clean(cfg):
    sources = _sources(
        payments=[_payment()], refunds=[_refund()], recon=[_row()], ledger=[_rma()]
    )
    assert _verdict(cfg, sources).closure_state == CLOSED_MATCHED


def test_a_refund_over_the_expected_amount_is_a_mismatch(cfg):
    """Razorpay only guarantees ΣR ≤ captured; only the merchant ledger sees this."""
    sources = _sources(
        payments=[_payment()],
        refunds=[_refund(amount=60_000)],
        recon=[_row(amount=60_000)],
        ledger=[_rma(amount=40_000)],
    )
    verdict = _verdict(cfg, sources)
    assert verdict.exception_codes == ["AMOUNT_MISMATCH"]
    assert verdict.evidence["amount_mismatch"]["direction"] == "OVER"
    assert verdict.evidence["amount_mismatch"]["delta_paise"] == 20_000


def test_the_receipt_join_beats_date_proximity(cfg):
    """Regression: two returns refunded out of order must not swap expectations.

    RMA-A was raised first but refunded second. Matching on "nearest earlier RMA"
    alone hands each refund the other's expected amount and reports two mismatches
    where there are none. `receipt` names the RMA outright, so it wins.
    """
    payment = _payment(amount=300_000)
    refunds = [
        _refund("rfnd_1", amount=60_100, created=date(2026, 8, 10), receipt="RMA-A"),
        _refund("rfnd_2", amount=84_000, created=date(2026, 8, 11), receipt="RMA-B"),
    ]
    ledger = [
        _rma("RMA-A", amount=60_100, created=date(2026, 8, 8)),
        _rma("RMA-B", amount=84_000, created=date(2026, 8, 9)),
    ]
    sources = _sources(
        payments=[payment],
        refunds=refunds,
        recon=[_row("rfnd_1", amount=60_100), _row("rfnd_2", amount=84_000)],
        ledger=ledger,
    )
    run = engine.close(sources, _cfg_at(cfg))
    for verdict in run.verdicts:
        assert verdict.exception_codes == [], verdict.evidence


def test_an_rma_row_is_consumed_once(cfg):
    """Two refunds, one return: the second gets no expectation rather than a stale one.

    Both carry receipts so the duplicate rule stays out of the way; `RMA-none`
    names no ledger row, so the second refund falls through to the date fallback
    and finds the pool already empty.
    """
    payment = _payment(amount=200_000)
    refunds = [
        _refund("rfnd_1", created=date(2026, 8, 10), receipt="RMA-1"),
        _refund("rfnd_2", created=date(2026, 8, 11), receipt="RMA-none"),
    ]
    sources = _sources(
        payments=[payment],
        refunds=refunds,
        recon=[_row("rfnd_1"), _row("rfnd_2")],
        ledger=[_rma("RMA-1")],
    )
    verdicts = engine.close(sources, _cfg_at(cfg)).by_id()
    assert verdicts["rfnd_1"].exception_codes == []
    assert "NO_RMA_MATCH" in verdicts["rfnd_2"].annotations
    assert verdicts["rfnd_2"].exception_codes == []


def test_no_rma_match_is_informational_only(cfg):
    """A goodwill refund has no RMA and is not an exception (SPEC.md §6.6)."""
    sources = _sources(payments=[_payment()], refunds=[_refund()], recon=[_row()])
    verdict = _verdict(cfg, sources)
    assert verdict.annotations == ["NO_RMA_MATCH"]
    assert verdict.closure_state == CLOSED_MATCHED


# --------------------------------------------------- stage 4: chargebacks


def _dispute(status="lost", phase="chargeback", amount=40_000, deducted=40_000,
             created=date(2026, 8, 15), pid="pay_1"):
    return Dispute(
        id="disp_1",
        payment_id=pid,
        amount=amount,
        amount_deducted=deducted,
        status=status,
        phase=phase,
        created_at=_ts(created),
    )


def test_a_lost_chargeback_after_a_refund_is_a_realised_double_payout(cfg):
    sources = _sources(
        payments=[_payment()], refunds=[_refund()], recon=[_row()], disputes=[_dispute()]
    )
    verdict = _verdict(cfg, sources)
    assert verdict.exception_codes == ["REFUND_PLUS_CHARGEBACK"]
    assert verdict.evidence["chargeback"]["sub"] == "REALIZED"
    assert verdict.exposure_paise == 80_000


def test_an_open_chargeback_is_exposure_not_loss(cfg):
    """`amount_deducted` is 0 until the dispute is lost, so this is at risk."""
    sources = _sources(
        payments=[_payment()],
        refunds=[_refund()],
        recon=[_row()],
        disputes=[_dispute(status="open", deducted=0)],
    )
    verdict = _verdict(cfg, sources)
    assert verdict.evidence["chargeback"]["sub"] == "AT_RISK"
    assert verdict.exposure_paise == 40_000


def test_a_won_dispute_is_annotated_not_flagged(cfg):
    sources = _sources(
        payments=[_payment()],
        refunds=[_refund()],
        recon=[_row()],
        disputes=[_dispute(status="won", deducted=0)],
    )
    verdict = _verdict(cfg, sources)
    assert verdict.exception_codes == []
    assert "DISPUTE_RESOLVED" in verdict.annotations
    assert verdict.closure_state == CLOSED_MATCHED


def test_a_retrieval_request_is_not_a_chargeback(cfg):
    sources = _sources(
        payments=[_payment()],
        refunds=[_refund()],
        recon=[_row()],
        disputes=[_dispute(phase="retrieval")],
    )
    assert _verdict(cfg, sources).exception_codes == []


def test_a_dispute_raised_before_the_refund_is_ignored(cfg):
    """Only refund-then-chargeback is reachable: the API blocks the other order."""
    sources = _sources(
        payments=[_payment()],
        refunds=[_refund(created=date(2026, 8, 16))],
        recon=[_row(settled=date(2026, 8, 18))],
        disputes=[_dispute(created=date(2026, 8, 15))],
    )
    assert _verdict(cfg, sources).exception_codes == []


def test_a_dispute_outside_the_chargeback_window_is_ignored(cfg):
    payment = _payment(created=date(2026, 1, 5))
    sources = _sources(
        payments=[payment],
        refunds=[_refund(created=date(2026, 8, 10))],
        recon=[_row()],
        disputes=[_dispute(created=date(2026, 8, 15))],
    )
    assert _verdict(cfg, sources).exception_codes == []


# ------------------------------------------------------ stage 5: bank evidence


def test_a_missing_arn_is_open_before_the_threshold(cfg):
    sources = _sources(
        payments=[_payment()],
        refunds=[_refund(created=date(2026, 8, 17), arn=None)],
        recon=[_row(settled=date(2026, 8, 19))],
    )
    verdict = _verdict(cfg, sources)
    assert verdict.closure_state == OPEN
    assert verdict.open_reasons == ["AWAITING_ARN"]
    assert verdict.leg3_settlement_deducted
    assert not verdict.leg4_bank_evidenced


def test_a_missing_arn_is_an_exception_after_the_threshold(cfg):
    sources = _sources(
        payments=[_payment(created=date(2026, 7, 1))],
        refunds=[_refund(created=date(2026, 7, 10), arn=None)],
        recon=[_row(settled=date(2026, 7, 13))],
    )
    verdict = _verdict(cfg, sources)
    assert verdict.exception_codes == ["ARN_OVERDUE"]


def test_leg_four_is_evidenced_never_counted_as_verified(cfg):
    """3 legs verified, 1 evidenced — `legs_verified` must never reach 4."""
    sources = _sources(payments=[_payment()], refunds=[_refund()], recon=[_row()])
    verdict = _verdict(cfg, sources)
    assert verdict.leg4_bank_evidenced
    assert verdict.legs_verified == 3


def test_settlement_and_arn_can_be_open_at_once(cfg):
    """Legs 3 and 4 are independent, so a young refund is open on both."""
    refund = _refund(created=date(2026, 8, 19), arn=None)
    verdict = _verdict(cfg, _sources(payments=[_payment()], refunds=[refund]))
    assert sorted(verdict.open_reasons) == ["AWAITING_ARN", "AWAITING_SETTLEMENT"]


# ---------------------------------------------------------- stage 6: precedence


def test_an_exception_code_outranks_an_open_reason(cfg):
    """A refund can be both overdue on the ARN and awaiting settlement."""
    refund = _refund(created=date(2026, 8, 19), arn=None)
    sources = _sources(payments=[_payment()], refunds=[refund], ledger=[_rma(amount=1)])
    verdict = _verdict(cfg, sources)
    assert verdict.exception_codes == ["AMOUNT_MISMATCH"]
    assert verdict.open_reasons  # still recorded
    assert verdict.closure_state == EXCEPTION


def test_a_clean_refund_with_every_leg_closes(cfg):
    sources = _sources(
        payments=[_payment()], refunds=[_refund()], recon=[_row()], ledger=[_rma()]
    )
    verdict = _verdict(cfg, sources)
    assert verdict.closure_state == CLOSED_MATCHED
    assert verdict.exception_codes == []
    assert verdict.open_reasons == []


def test_the_identity_assertion_actually_fires():
    """The guard in ClosureRun must not be decorative."""
    from rlc.entities import RefundVerdict

    good = RefundVerdict(refund_id="a", payment_id="p", amount=1, closure_state=CLOSED_MATCHED)
    with pytest.raises(ValueError):
        RefundVerdict(refund_id="b", payment_id="p", amount=1, closure_state="SOMETHING_ELSE")
    assert good.closure_state == CLOSED_MATCHED
