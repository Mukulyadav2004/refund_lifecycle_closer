"""The closure state machine (spec §6, CLAUDE.md §6).

Every refund resolves to exactly one `closure_state`; the run asserts
`N_in == N_closed + N_open + N_exception + N_rejected` and fails if it does not
hold. Zero silent drops is a property of this module, not a hope about it.

Nothing here is probabilistic and nothing here calls a model. Stages 0-3 and 5
are pure arithmetic and date comparison. Stage 4 contains the single heuristic
in the whole project — the duplicate rule — which is why it alone attaches a
confidence and sets `needs_human_review`. The LLM never sees a decision; it only
writes prose about decisions already made (spec §9).

This module must not import `rlc.evaluate` or read `ground_truth.json`.

Two boundaries worth stating because they are easy to blur:

* Leakage and timing are attributes of every record and are computed in
  `rlc.attributes`, not here. They are not states and not buckets.
* The maturity gate in stage 2 is not optional. Without it every young refund
  becomes a false `NEVER_DEDUCTED` and precision collapses.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping

from .calendar_utils import BankCalendar, to_ist_date
from .config import Config
from .entities import (
    CLOSED_MATCHED,
    EXCEPTION,
    OPEN,
    REJECTED_INPUT,
    Payment,
    Refund,
    RefundVerdict,
    ReturnsLedgerRow,
)
from .invariants import IntegrityReport
from .invariants import run as run_invariants
from .loader import Sources

SECONDS_PER_DAY = 86_400

# Spec §6.6: a retrieval request is not a chargeback, so it never counts as a
# double payout. The other four phases all end in money leaving the merchant.
CHARGEBACK_PHASES = frozenset({"fraud", "chargeback", "pre_arbitration", "arbitration"})

# Emitted by the engine but not seeded by the generator: a refund with no
# matching RMA row may simply be a goodwill refund (spec §6.6). Informational in
# v1, so the evaluator must not score it as a false positive.
INFORMATIONAL_ANNOTATIONS = frozenset({"NO_RMA_MATCH"})

# Duplicate confidence bands (spec §6.6). Absolute, not relative to the window,
# so widening the window adds only low-confidence pairs.
_CONFIDENCE_BANDS = ((1_800, 0.9), (SECONDS_PER_DAY, 0.7))
_CONFIDENCE_FLOOR = 0.5


@dataclass(frozen=True, slots=True)
class ClosureRun:
    """The output of one pass over the pull, plus what it took to produce it."""

    verdicts: tuple[RefundVerdict, ...]
    integrity: IntegrityReport
    duplicate_window_seconds: int
    as_of: date
    elapsed_seconds: float

    def __post_init__(self) -> None:
        counts = self.state_counts
        total = sum(counts.values())
        if total != len(self.verdicts):
            raise AssertionError(
                f"identity broken: {len(self.verdicts)} refunds in, {total} states out "
                f"({counts}). Every refund must resolve to exactly one closure_state."
            )

    @property
    def state_counts(self) -> dict[str, int]:
        counts = Counter(v.closure_state for v in self.verdicts)
        return {
            CLOSED_MATCHED: counts.get(CLOSED_MATCHED, 0),
            OPEN: counts.get(OPEN, 0),
            EXCEPTION: counts.get(EXCEPTION, 0),
            REJECTED_INPUT: counts.get(REJECTED_INPUT, 0),
        }

    @property
    def code_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for v in self.verdicts:
            counts.update(v.exception_codes)
        return dict(sorted(counts.items()))

    @property
    def open_reason_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for v in self.verdicts:
            counts.update(v.open_reasons)
        return dict(sorted(counts.items()))

    @property
    def records_per_second(self) -> float:
        return len(self.verdicts) / self.elapsed_seconds if self.elapsed_seconds else 0.0

    def by_id(self) -> dict[str, RefundVerdict]:
        return {v.refund_id: v for v in self.verdicts}


# ------------------------------------------------------------- returns ledger


def match_rmas(
    sources: Sources, eligible: Iterable[str]
) -> dict[str, ReturnsLedgerRow | None]:
    """Bind each eligible refund to at most one RMA row (spec §6.6).

    Two passes, exact before approximate, because `AMOUNT_MISMATCH` is only as
    trustworthy as this join:

    1. `refund.receipt == rma_id`. `receipt` is the merchant's own per-payment
       idempotency key (CLAUDE.md §3), and a merchant whose OMS stamps the RMA
       id into it has handed us an exact key. Using a date heuristic in its
       presence invents mismatches: two returns raised a day apart, refunded out
       of order, get each other's expected amounts and both look wrong.
    2. Nearest unconsumed row with `rma_created_at <= refund.created_at`, for
       refunds with no receipt or a receipt that names no RMA. Exact matches are
       claimed first so this fallback can never steal a row that belonged to a
       refund naming it outright.

    Each row is consumed once, so a payment with two returns and two refunds is
    matched pairwise instead of scoring both against the same expectation.
    Refunds are walked in `(created_at, id)` order, so the result does not depend
    on file ordering.
    """
    eligible_ids = set(eligible)
    ordered = sorted(
        (r for r in sources.refunds if r.id in eligible_ids),
        key=lambda r: (r.created_at, r.id),
    )
    pools: dict[str, list[ReturnsLedgerRow]] = {}

    def pool_for(payment_id: str | None) -> list[ReturnsLedgerRow]:
        key = payment_id or ""
        if key not in pools:
            pools[key] = list(sources.rmas_of(payment_id))
        return pools[key]

    matches: dict[str, ReturnsLedgerRow | None] = {}

    # Pass 1 — exact receipt join.
    for refund in ordered:
        if not refund.receipt:
            continue
        pool = pool_for(refund.payment_id)
        exact = next((row for row in pool if row.rma_id == refund.receipt), None)
        if exact is not None:
            pool.remove(exact)
            matches[refund.id] = exact

    # Pass 2 — nearest unconsumed row, for whatever is left.
    for refund in ordered:
        if refund.id in matches:
            continue
        pool = pool_for(refund.payment_id)
        candidates = [row for row in pool if row.rma_created_at <= refund.created_at]
        if not candidates:
            matches[refund.id] = None
            continue
        chosen = max(candidates, key=lambda row: (row.rma_created_at, row.rma_id))
        pool.remove(chosen)
        matches[refund.id] = chosen
    return matches


def _duplicate_confidence(delta_seconds: int) -> float:
    for bound, confidence in _CONFIDENCE_BANDS:
        if delta_seconds <= bound:
            return confidence
    return _CONFIDENCE_FLOOR


# --------------------------------------------------------------- state machine


def classify(
    refund: Refund,
    sources: Sources,
    cfg: Config,
    calendar: BankCalendar,
    integrity: IntegrityReport,
    rma: ReturnsLedgerRow | None,
    duplicate_window_seconds: int,
) -> RefundVerdict:
    """Run stages 0-6 for one refund and return its verdict (spec §6)."""
    now = cfg.run.as_of
    thresholds = cfg.thresholds
    parent: Payment | None = sources.parent_of(refund)

    codes: list[str] = []
    open_reasons: list[str] = []
    annotations: list[str] = []
    evidence: dict[str, Any] = {}
    exposure = 0
    confidence: float | None = None
    needs_human_review = False
    settlement_id: str | None = None
    settlement_utr: str | None = None
    leg3 = False
    leg4 = False

    # --- Stage 0: integrity (spec §6.2) ------------------------------------
    rejection_reasons = integrity.reasons_for(refund.id)
    if rejection_reasons:
        # Stop here. Legs 2-4 stay False because they were never checked, not
        # because they failed — a rejected input is a data problem, and claiming
        # a verified leg on it would overstate what the run actually proved.
        return RefundVerdict(
            refund_id=refund.id,
            payment_id=refund.payment_id,
            amount=refund.amount,
            closure_state=REJECTED_INPUT,
            rejection_reasons=rejection_reasons,
            evidence={"stage": "0_integrity"},
        )

    created_d = to_ist_date(refund.created_at)
    settle_due = calendar.add_working_days(created_d, thresholds.settle_threshold_wd)
    arn_due = calendar.add_working_days(created_d, thresholds.arn_threshold_wd)
    pending_due = calendar.add_working_days(created_d, thresholds.pending_threshold_wd)
    siblings = sources.sibling_refunds(refund.payment_id)

    # --- Stage 1: status (spec §6.3) ---------------------------------------
    if refund.status == "failed":
        # R(P) already excludes failed refunds, so `replacement` is by
        # construction a refund that actually moved money.
        replacement = next(
            (
                r
                for r in sorted(siblings, key=lambda r: (r.created_at, r.id))
                if r.amount == refund.amount and r.created_at > refund.created_at
            ),
            None,
        )
        if replacement is not None:
            annotations.append("FAILED_SUPERSEDED")
            evidence = {"stage": "1_status", "superseded_by": replacement.id}
            state = CLOSED_MATCHED
        else:
            codes.append("REFUND_FAILED")
            evidence = {"stage": "1_status", "detail": "failed with no successful re-issue"}
            state = EXCEPTION
        return RefundVerdict(
            refund_id=refund.id,
            payment_id=refund.payment_id,
            amount=refund.amount,
            closure_state=state,
            exception_codes=codes,
            annotations=annotations,
            evidence=evidence,
        )

    skip_settlement_leg = False
    if refund.status == "pending":
        # No settlement is expected for a refund the gateway has not processed,
        # so stages 2, 3 and 5 are skipped. Stage 4 still runs: a duplicate or a
        # chargeback overlap is visible before the gateway finishes.
        skip_settlement_leg = True
        if now > pending_due:
            codes.append("PENDING_OVERDUE")
            evidence["pending_due"] = pending_due.isoformat()
        else:
            open_reasons.append("AWAITING_PROCESSING")

    # --- Stages 2 and 3: the settlement leg (spec §6.4, §6.5) --------------
    if not skip_settlement_leg:
        rows = integrity.settlement_rows(refund.id)
        if len(rows) == 0:
            if now < settle_due:
                # The maturity gate. A refund created yesterday has not failed to
                # settle; it has not had the chance to.
                open_reasons.append("AWAITING_SETTLEMENT")
                evidence["settle_due"] = settle_due.isoformat()
            else:
                codes.append("NEVER_DEDUCTED")
                evidence["settle_due"] = settle_due.isoformat()
        elif len(rows) > 1:
            codes.append("DOUBLE_DEDUCTED")
            evidence["double_deducted"] = {
                "rows": len(rows),
                "settlement_ids": [row.settlement_id for row in rows],
                "total_debit": sum(row.debit for row in rows),
            }
        else:
            row = rows[0]
            settlement_id = row.settlement_id or None
            settlement_utr = row.settlement_utr
            if row.debit != refund.amount or row.credit != 0:
                codes.append("SETTLEMENT_AMOUNT_DELTA")
                evidence["settlement_delta"] = {
                    "debit": row.debit,
                    "credit": row.credit,
                    "delta": row.debit - refund.amount,
                }
            else:
                # Leg 3 verified: exactly one row, debit equals the refund, and
                # nothing was credited back (spec §5).
                leg3 = True

    # --- Stage 4: cross-record checks on the parent (spec §6.6) ------------
    # Duplicate — the only heuristic rule in the engine.
    prior_twins = [
        other
        for other in siblings
        if other.id != refund.id
        and other.amount == refund.amount
        and refund.created_at > other.created_at
        and refund.created_at - other.created_at <= duplicate_window_seconds
        # Two refunds with distinct receipts are two refunds Razorpay accepted
        # under an idempotency key. Legitimate multi-partial refunds live here,
        # and this clause is the only thing keeping them out of the exceptions.
        and (refund.receipt is None or other.receipt is None)
    ]
    if prior_twins:
        twin = min(prior_twins, key=lambda o: (refund.created_at - o.created_at, o.id))
        delta = refund.created_at - twin.created_at
        codes.append("DUPLICATE_SUSPECT")
        confidence = _duplicate_confidence(delta)
        needs_human_review = True
        evidence["duplicate"] = {
            "twin_refund_id": twin.id,
            "delta_seconds": delta,
            "window_seconds": duplicate_window_seconds,
            "receipts": [refund.receipt, twin.receipt],
        }

    # Amount mismatch — computable only because the merchant's own RMA ledger
    # exists. Razorpay guarantees ΣR <= captured and nothing about correctness.
    if rma is None:
        annotations.append("NO_RMA_MATCH")
    else:
        delta = refund.amount - rma.expected_refund_paise
        if abs(delta) > thresholds.amount_tolerance_paise:
            codes.append("AMOUNT_MISMATCH")
            evidence["amount_mismatch"] = {
                "rma_id": rma.rma_id,
                "expected_paise": rma.expected_refund_paise,
                "delta_paise": delta,
                "direction": "OVER" if delta > 0 else "UNDER",
            }

    # Refund plus chargeback. Only the refund-first ordering is reachable: a
    # refund attempted during a dispute is blocked at the API (F8).
    if parent is not None:
        window_seconds = thresholds.chargeback_window_days * SECONDS_PER_DAY
        realized: list[str] = []
        at_risk: list[str] = []
        for dispute in sources.disputes_of(refund.payment_id):
            if dispute.created_at <= refund.created_at:
                continue
            if dispute.created_at - parent.created_at > window_seconds:
                continue
            if dispute.phase not in CHARGEBACK_PHASES:
                continue
            if dispute.is_realized_loss:
                realized.append(dispute.id)
                exposure += refund.amount + dispute.amount_deducted
            elif dispute.is_at_risk:
                at_risk.append(dispute.id)
                exposure += dispute.amount
            else:
                # Won or closed: the money came back, so there is no double
                # payout to report — but say so, rather than staying silent.
                annotations.append("DISPUTE_RESOLVED")
        if realized or at_risk:
            codes.append("REFUND_PLUS_CHARGEBACK")
            evidence["chargeback"] = {
                "sub": "REALIZED" if realized else "AT_RISK",
                "realized_dispute_ids": realized,
                "at_risk_dispute_ids": at_risk,
                "exposure_paise": exposure,
            }

    # --- Stage 5: bank-side evidence (spec §6.7) ---------------------------
    # Processed only, and it runs even when stage 2 left the refund awaiting
    # settlement: the two legs are independent, so a young refund can be open on
    # both at once.
    if refund.status == "processed":
        if refund.arn is None:
            if now < arn_due:
                open_reasons.append("AWAITING_ARN")
                evidence["arn_due"] = arn_due.isoformat()
            else:
                codes.append("ARN_OVERDUE")
                evidence["arn_due"] = arn_due.isoformat()
        else:
            # Evidenced, never verified: an ARN proves a bank reference was
            # issued, not that the customer's account was credited (spec §5).
            leg4 = True

    # --- Stage 6: decide (spec §6.8) ---------------------------------------
    if codes:
        state = EXCEPTION
    elif open_reasons:
        state = OPEN
    else:
        state = CLOSED_MATCHED

    return RefundVerdict(
        refund_id=refund.id,
        payment_id=refund.payment_id,
        amount=refund.amount,
        closure_state=state,
        exception_codes=codes,
        open_reasons=open_reasons,
        annotations=annotations,
        exposure_paise=exposure,
        settlement_id=settlement_id,
        settlement_utr=settlement_utr,
        leg2_gateway_processed=refund.status == "processed",
        leg3_settlement_deducted=leg3,
        leg4_bank_evidenced=leg4,
        confidence=confidence,
        needs_human_review=needs_human_review,
        evidence=evidence,
    )


def close(
    sources: Sources,
    cfg: Config,
    integrity: IntegrityReport | None = None,
    calendar: BankCalendar | None = None,
    duplicate_window_seconds: int | None = None,
) -> ClosureRun:
    """Close every refund in the pull (spec §6).

    `duplicate_window_seconds` overrides the configured window so the evaluator
    can report duplicate sensitivity at 30 min / 24 h / 72 h without a second
    load.
    """
    started = time.perf_counter()
    cal = calendar or cfg.calendar()
    report = integrity if integrity is not None else run_invariants(sources, cfg)
    window = (
        duplicate_window_seconds
        if duplicate_window_seconds is not None
        else cfg.thresholds.duplicate_window_seconds
    )

    # Stage 4 is reached only by refunds that clear stage 0 and are not failed,
    # so only those may consume an RMA row.
    eligible = [
        r.id
        for r in sources.refunds
        if not report.is_rejected(r.id) and r.status != "failed"
    ]
    rmas = match_rmas(sources, eligible)

    verdicts = tuple(
        classify(refund, sources, cfg, cal, report, rmas.get(refund.id), window)
        for refund in sources.refunds
    )
    return ClosureRun(
        verdicts=verdicts,
        integrity=report,
        duplicate_window_seconds=window,
        as_of=cfg.run.as_of,
        elapsed_seconds=time.perf_counter() - started,
    )
