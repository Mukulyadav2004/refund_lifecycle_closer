"""Integrity invariants I1-I7 (spec §5) — assert, never assume.

Violations go to a `data_errors` channel: counted, reported, and excluded from
the match-rate denominator with the exclusion stated. They never enter exception
counts, because an exception is a merchant problem and an integrity failure is a
*data* problem — usually a pull window that is too narrow.

The distinction the vocabulary exists to protect (CLAUDE.md §1):

* `NO_PARENT_PAYMENT` is an integrity rejection. Razorpay cannot create a refund
  without a captured parent, so a missing parent means the pull is wrong.
* `NEVER_DEDUCTED` is a real merchant exception, decided by the state machine.

Neither is called an "orphan".
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .calendar_utils import ist_unix
from .config import Config
from .entities import Payment, ReconRow, Refund
from .loader import Sources

# The floor of I7. Razorpay ids predate nothing useful before this.
EPOCH_FLOOR = date(2000, 1, 1)

DATA_ERROR_CHANNELS = (
    "JOIN_CORRUPTION",            # I4: recon refund row pointing at the wrong payment
    "RECON_ROW_UNMATCHED",        # recon refund row for a refund outside the pull
    "RECON_FEE_NONZERO",          # I5: a refund row must carry fee = 0 and tax = 0
    "AMOUNT_REFUNDED_MISMATCH",   # I3: payment.amount_refunded != ΣR
    "REFUND_STATUS_INCONSISTENT", # I3: refund_status disagrees with ΣR
    "SETTLEMENT_CONTROL_BREAK",   # I6: Σcredit - Σdebit != settlement.amount
)


@dataclass(frozen=True, slots=True)
class DataError:
    """One integrity violation, addressed to whoever owns the upstream data."""

    channel: str
    entity_id: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class IntegrityReport:
    """What the engine is allowed to believe about the pull.

    `rejections` is the stage-0 input to the state machine. `settlement_rows`
    is the S(r) map with I4-corrupt rows already removed — the engine never sees
    a row that failed the join check, which is what gives I4 teeth.
    """

    rejections: dict[str, list[str]] = field(default_factory=dict)
    data_errors: list[DataError] = field(default_factory=list)
    payment_anomalies: dict[str, list[str]] = field(default_factory=dict)
    _settlement_rows: dict[str, tuple[ReconRow, ...]] = field(default_factory=dict)

    def settlement_rows(self, refund_id: str) -> tuple[ReconRow, ...]:
        return self._settlement_rows.get(refund_id, ())

    def is_rejected(self, refund_id: str) -> bool:
        return refund_id in self.rejections

    def reasons_for(self, refund_id: str) -> list[str]:
        return list(self.rejections.get(refund_id, ()))

    @property
    def channel_counts(self) -> dict[str, int]:
        counts = Counter(e.channel for e in self.data_errors)
        return {channel: counts.get(channel, 0) for channel in DATA_ERROR_CHANNELS}

    @property
    def rejection_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for reasons in self.rejections.values():
            counts.update(reasons)
        return dict(sorted(counts.items()))


def _reject(report: IntegrityReport, refund_id: str, reason: str) -> None:
    reasons = report.rejections.setdefault(refund_id, [])
    if reason not in reasons:
        reasons.append(reason)


def check_refund(
    refund: Refund,
    parent: Payment | None,
    lower_bound: int,
    upper_bound: int,
) -> list[str]:
    """I1 and I7 for a single refund. Returns rejection reasons, possibly empty."""
    reasons: list[str] = []

    if not lower_bound <= refund.created_at <= upper_bound:
        reasons.append("TIMESTAMP_OUT_OF_RANGE")
    if refund.amount <= 0:
        reasons.append("NEGATIVE_AMOUNT")
    if parent is None:
        # Covers both "payment_id absent" and "payment_id present but not found".
        reasons.append("NO_PARENT_PAYMENT")
        return reasons

    if refund.currency != parent.currency:
        reasons.append("CURRENCY_MISMATCH")
    if refund.created_at < parent.created_at:
        reasons.append("REFUND_BEFORE_PAYMENT")
    if parent.status not in ("captured", "refunded"):
        reasons.append("PARENT_NOT_CAPTURED")
    return reasons


def run(sources: Sources, cfg: Config) -> IntegrityReport:
    """Apply I1-I7 across the whole pull (spec §5)."""
    report = IntegrityReport()
    lower = ist_unix(EPOCH_FLOOR)
    upper = ist_unix(cfg.run.as_of, 23, 59, 59)

    # --- I1, I7: per refund -------------------------------------------------
    for refund in sources.refunds:
        for reason in check_refund(refund, sources.parent_of(refund), lower, upper):
            _reject(report, refund.id, reason)

    # --- I2: ΣR must not exceed the captured amount -------------------------
    # Razorpay refuses a refund above the captured amount (F4), so this is
    # impossible in real data: seeing it means the pull is corrupt, not that the
    # merchant over-refunded. Refunds already rejected are left out of ΣR so a
    # negative amount cannot mask a genuine over-capture.
    for payment_id, siblings in sources.refunds_by_payment.items():
        parent = sources.payments_by_id.get(payment_id)
        if parent is None:
            continue
        counted = [
            r for r in siblings if r.counts_toward_parent_total and not report.is_rejected(r.id)
        ]
        total = sum(r.amount for r in counted)
        if total > parent.amount:
            for r in siblings:
                _reject(report, r.id, "OVER_CAPTURED_TOTAL")
            report.data_errors.append(
                DataError(
                    channel="JOIN_CORRUPTION",
                    entity_id=payment_id,
                    detail=f"ΣR {total} exceeds captured {parent.amount}",
                    evidence={"refund_ids": [r.id for r in siblings]},
                )
            )

    # --- I3: amount_refunded and refund_status must agree with ΣR -----------
    # An anomaly note on the payment. The refunds themselves are still processed:
    # the money moved, whatever the summary field says.
    for payment in sources.payments:
        siblings = sources.refunds_by_payment.get(payment.id, ())
        # Rejected refunds are excluded from ΣR here exactly as they are in I2.
        # Without that, a payment carrying a seeded integrity rejection reports a
        # false anomaly: its `amount_refunded` correctly ignores the bad refund,
        # and summing a negative amount into ΣR is meaningless anyway.
        total = sum(
            r.amount
            for r in siblings
            if r.counts_toward_parent_total and not report.is_rejected(r.id)
        )
        if payment.amount_refunded != total:
            report.payment_anomalies.setdefault(payment.id, []).append("AMOUNT_REFUNDED_MISMATCH")
            report.data_errors.append(
                DataError(
                    channel="AMOUNT_REFUNDED_MISMATCH",
                    entity_id=payment.id,
                    detail=f"amount_refunded {payment.amount_refunded} != ΣR {total}",
                )
            )
        expected_status = (
            "full" if total == payment.amount and total > 0
            else "partial" if 0 < total < payment.amount
            else None
        )
        if payment.refund_status != expected_status:
            report.payment_anomalies.setdefault(payment.id, []).append("REFUND_STATUS_INCONSISTENT")
            report.data_errors.append(
                DataError(
                    channel="REFUND_STATUS_INCONSISTENT",
                    entity_id=payment.id,
                    detail=f"refund_status {payment.refund_status!r} != {expected_status!r} for ΣR {total}",
                )
            )

    # --- I4, I5: recon refund rows ------------------------------------------
    clean: dict[str, list[ReconRow]] = defaultdict(list)
    for refund_id, rows in sources.recon_refunds_by_refund_id.items():
        refund = sources.refunds_by_id.get(refund_id)
        if refund is None:
            report.data_errors.append(
                DataError(
                    channel="RECON_ROW_UNMATCHED",
                    entity_id=refund_id,
                    detail="recon refund row has no refund in the pull",
                    evidence={"rows": len(rows)},
                )
            )
            continue
        for row in rows:
            if row.payment_id != refund.payment_id:
                # I4: the row is ignored entirely, so it can neither satisfy the
                # settlement leg nor inflate a DOUBLE_DEDUCTED count.
                report.data_errors.append(
                    DataError(
                        channel="JOIN_CORRUPTION",
                        entity_id=refund_id,
                        detail=(
                            f"recon row payment_id {row.payment_id!r} != refund payment_id "
                            f"{refund.payment_id!r}; row ignored"
                        ),
                        evidence={"settlement_id": row.settlement_id},
                    )
                )
                continue
            if row.fee != 0 or row.tax != 0:
                # I5: note it, do not interpret it. A refund row carries no fee
                # (F13); the leakage is on the payment row (spec §7.1).
                report.data_errors.append(
                    DataError(
                        channel="RECON_FEE_NONZERO",
                        entity_id=refund_id,
                        detail=f"refund recon row has fee={row.fee} tax={row.tax}, expected 0/0",
                    )
                )
            clean[refund_id].append(row)
    report._settlement_rows = {k: tuple(v) for k, v in clean.items()}

    # --- I6: settlement control totals --------------------------------------
    # Reported at batch level, never pushed down onto individual refunds.
    for settlement in sources.settlements:
        rows = sources.recon_by_settlement.get(settlement.id, ())
        net = sum(row.net for row in rows)
        if net != settlement.amount:
            report.data_errors.append(
                DataError(
                    channel="SETTLEMENT_CONTROL_BREAK",
                    entity_id=settlement.id,
                    detail=f"Σcredit - Σdebit = {net}, settlement.amount = {settlement.amount}",
                    evidence={"rows": len(rows), "delta": net - settlement.amount},
                )
            )

    return report
