"""Leakage, timing and leg evidence (spec §7, CLAUDE.md §7).

These are **attributes computed on every non-rejected record**, matched ones
included. They are not states and not buckets. Leakage applies to essentially
every refund, so routing a refund into an "exception bucket" would remove it
from the leakage total and break the numbers — which is exactly why closure
state and leakage are separate axes (CLAUDE.md §1).

The one calculation everybody gets wrong is §7.1. The recon *refund* row carries
`fee = 0, tax = 0`; the money the merchant lost is the original *payment* row's
fee, which is already GST-inclusive. So:

* Never compute `fee + tax` — that double-counts the GST.
* Never round per refund. Three partials on one payment, rounded independently,
  miss the parent fee by 1-2 paise and the control total stops tying. Allocate
  with largest remainder across all non-failed refunds of the payment instead.

Nothing here reads `ground_truth.json`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING

from .calendar_utils import BankCalendar, month_key, to_ist_date
from .config import Config
from .entities import REJECTED_INPUT, Payment, Refund, RefundVerdict
from .invariants import IntegrityReport
from .loader import Sources
from .money import allocate_largest_remainder, bps_of

if TYPE_CHECKING:  # pragma: no cover
    from .engine import ClosureRun


# --------------------------------------------------------------------- totals


@dataclass(frozen=True, slots=True)
class LeakageTotals:
    """What the month's refunds cost the merchant in fees they never earn back.

    `total_paise` is GST-inclusive and always equals `gst_paise + mdr_paise`.
    """

    total_paise: int
    gst_paise: int
    mdr_paise: int
    instant_fee_paise: int
    refunded_paise: int
    records: int
    by_method: dict[str, dict[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.total_paise != self.gst_paise + self.mdr_paise:
            raise AssertionError(
                f"leakage split broken: {self.total_paise} != {self.gst_paise} + "
                f"{self.mdr_paise}. `fee` on the Payment entity is GST-inclusive."
            )

    @property
    def leakage_bps(self) -> int:
        """Leakage as basis points of the refunded principal (~236 bps under A1)."""
        return bps_of(self.total_paise, self.refunded_paise)


@dataclass(frozen=True, slots=True)
class TimingTotals:
    """Lag reported as a distribution. No SLA is asserted, because none exists."""

    measured: int
    cross_period_count: int
    cross_period_paise: int
    late_vs_threshold_count: int
    lag_histogram: dict[int, int] = field(default_factory=dict)

    @property
    def median_lag_wd(self) -> int | None:
        if not self.lag_histogram:
            return None
        ordered = [lag for lag, n in sorted(self.lag_histogram.items()) for _ in range(n)]
        return ordered[len(ordered) // 2]


@dataclass(frozen=True, slots=True)
class LegTotals:
    """Three legs verified, one evidenced. Never sum all four (spec §5)."""

    records: int
    initiated: int
    gateway_processed: int
    settlement_deducted: int
    bank_evidenced: int


@dataclass(frozen=True, slots=True)
class SettlementControl:
    """Spec §8: every paisa of debit-versus-refund difference must be explained.

    `unexplained_paise` is the number that has to be zero. If it is not, some
    settlement discrepancy exists that no exception code accounts for, and the
    report would be quietly understating the problem.
    """

    debit_paise: int
    refund_amount_paise: int
    explained_by_amount_delta_paise: int
    explained_by_double_deduction_paise: int

    @property
    def difference_paise(self) -> int:
        return self.debit_paise - self.refund_amount_paise

    @property
    def unexplained_paise(self) -> int:
        return (
            self.difference_paise
            - self.explained_by_amount_delta_paise
            - self.explained_by_double_deduction_paise
        )


@dataclass(frozen=True, slots=True)
class AttributeTotals:
    leakage: LeakageTotals
    timing: TimingTotals
    legs: LegTotals
    control: SettlementControl
    rounding_residual_paise: int


# ------------------------------------------------------------------- leakage


def allocate_payment_leakage(
    payment: Payment, refunds: list[Refund]
) -> dict[str, tuple[int, int]]:
    """Split a payment's GST-inclusive fee across its refunds (spec §7.1).

    Returns `{refund_id: (fee_share, gst_share)}`. `refunds` must already exclude
    failed and rejected refunds: a failed refund moved no money, and a rejected
    one is a data error, so neither carries any of the parent's fee.

    Both allocations run against the same parts and base, so the GST share of a
    refund never exceeds its fee share, and both sum to the floor of the exact
    total on the refunded portion.
    """
    if not refunds:
        return {}
    ordered = sorted(refunds, key=lambda r: (r.created_at, r.id))
    parts = [r.amount for r in ordered]
    fee_alloc = allocate_largest_remainder(payment.fee, parts, payment.amount)
    tax_alloc = allocate_largest_remainder(payment.tax, parts, payment.amount)

    refunded = sum(parts)
    # The allocation is exact by construction: whatever rounding happens is
    # absorbed inside the payment, never accumulated across payments.
    assert sum(fee_alloc) == (Fraction(payment.fee * refunded, payment.amount)).__floor__(), (
        f"{payment.id}: fee allocation {sum(fee_alloc)} does not equal the exact "
        "floor of the refunded share — largest-remainder allocation is broken"
    )
    return {r.id: (fee_alloc[i], tax_alloc[i]) for i, r in enumerate(ordered)}


def _leakage_inputs(
    sources: Sources, verdicts: dict[str, RefundVerdict]
) -> dict[str, list[Refund]]:
    """Non-failed, non-rejected refunds per payment, which is what carries fee."""
    grouped: dict[str, list[Refund]] = defaultdict(list)
    for refund in sources.refunds:
        verdict = verdicts.get(refund.id)
        if verdict is None or verdict.closure_state == REJECTED_INPUT:
            continue
        if not refund.counts_toward_parent_total:
            continue
        if refund.payment_id and refund.payment_id in sources.payments_by_id:
            grouped[refund.payment_id].append(refund)
    return grouped


# ------------------------------------------------------------------- annotate


def annotate(
    run: "ClosureRun",
    sources: Sources,
    cfg: Config,
    calendar: BankCalendar | None = None,
    integrity: IntegrityReport | None = None,
) -> AttributeTotals:
    """Fill in leakage, timing and lag on every verdict, and return the totals.

    Verdicts are enriched in place: `RefundVerdict` carries these fields with
    neutral defaults precisely so the state machine and the money arithmetic stay
    separate modules. Rejected records are skipped — a refund whose parent is
    missing has no fee to pro-rate and no settlement date to measure against.
    """
    cal = calendar or cfg.calendar()
    report = integrity if integrity is not None else run.integrity
    verdicts = run.by_id()
    threshold = cfg.thresholds.settle_threshold_wd
    instant_fee = cfg.pricing.instant_refund_fee_paise

    # --- leakage (spec §7.1) ------------------------------------------------
    allocations: dict[str, tuple[int, int]] = {}
    for payment_id, refunds in _leakage_inputs(sources, verdicts).items():
        allocations.update(allocate_payment_leakage(sources.payments_by_id[payment_id], refunds))

    total = gst = mdr = instant_total = refunded = records = 0
    by_method: dict[str, dict[str, int]] = defaultdict(
        lambda: {"leakage_paise": 0, "refunded_paise": 0, "records": 0}
    )
    exact_sum = Fraction(0)

    # --- timing (spec §7.2) -------------------------------------------------
    measured = cross_count = cross_paise = late_count = 0
    histogram: Counter[int] = Counter()

    # --- settlement control (spec §8) --------------------------------------
    debit_total = amount_total = delta_explained = double_explained = 0

    for verdict in run.verdicts:
        refund = sources.refunds_by_id[verdict.refund_id]
        rows = report.settlement_rows(refund.id)

        # Control total covers every settled refund, including rejected ones:
        # the money moved whatever the engine decided about the record. The
        # refund amount is counted once per refund and the debits once per row —
        # that asymmetry is the point, since it is what surfaces a second debit.
        if rows:
            amount_total += refund.amount
            debit_total += sum(row.debit for row in rows)
        if "SETTLEMENT_AMOUNT_DELTA" in verdict.exception_codes and len(rows) == 1:
            delta_explained += rows[0].debit - refund.amount
        if "DOUBLE_DEDUCTED" in verdict.exception_codes:
            double_explained += sum(row.debit for row in rows) - refund.amount

        if verdict.closure_state == REJECTED_INPUT:
            continue

        fee_share, gst_share = allocations.get(refund.id, (0, 0))
        # Instant-refund fee: charged only when the refund actually completed
        # instantly. If `optimum` was requested but processed at normal speed,
        # Razorpay credits the fee back, so it is not leakage (F9). Its GST
        # treatment is not documented, so the whole amount is booked as MDR
        # rather than inventing a split.
        extra = instant_fee if refund.instant_fee_applies else 0
        verdict.leakage_paise = fee_share + extra
        verdict.leakage_gst_paise = gst_share
        verdict.leakage_mdr_paise = fee_share - gst_share + extra

        if refund.id in allocations or extra:
            total += verdict.leakage_paise
            gst += verdict.leakage_gst_paise
            mdr += verdict.leakage_mdr_paise
            instant_total += extra
            refunded += refund.amount
            records += 1
            parent = sources.parent_of(refund)
            method = parent.method if parent else "unknown"
            bucket = by_method[method]
            bucket["leakage_paise"] += verdict.leakage_paise
            bucket["refunded_paise"] += refund.amount
            bucket["records"] += 1
            if parent is not None and parent.amount:
                exact_sum += Fraction(parent.fee * refund.amount, parent.amount)

        # Timing needs an unambiguous settlement date, so it is measured only
        # where exactly one recon row claims the refund (spec §7.2).
        if len(rows) == 1:
            created_d = to_ist_date(refund.created_at)
            settled_d = to_ist_date(rows[0].settled_at)
            lag = cal.working_days_between(created_d, settled_d)
            verdict.settle_lag_wd = lag
            measured += 1
            histogram[lag] += 1
            if month_key(created_d) != month_key(settled_d):
                # Binary and assumption-free: this is exactly the "an August
                # refund reduces September's settlement" problem.
                verdict.timing_flags.append("CROSS_PERIOD")
                cross_count += 1
                cross_paise += refund.amount
            if lag > threshold:
                verdict.timing_flags.append("LATE_VS_THRESHOLD")
                late_count += 1

    leakage = LeakageTotals(
        total_paise=total,
        gst_paise=gst,
        mdr_paise=mdr,
        instant_fee_paise=instant_total,
        refunded_paise=refunded,
        records=records,
        by_method={k: dict(v) for k, v in sorted(by_method.items())},
    )
    timing = TimingTotals(
        measured=measured,
        cross_period_count=cross_count,
        cross_period_paise=cross_paise,
        late_vs_threshold_count=late_count,
        lag_histogram=dict(sorted(histogram.items())),
    )
    legs = LegTotals(
        records=len(run.verdicts),
        initiated=sum(1 for v in run.verdicts if v.leg1_initiated),
        gateway_processed=sum(1 for v in run.verdicts if v.leg2_gateway_processed),
        settlement_deducted=sum(1 for v in run.verdicts if v.leg3_settlement_deducted),
        bank_evidenced=sum(1 for v in run.verdicts if v.leg4_bank_evidenced),
    )
    control = SettlementControl(
        debit_paise=debit_total,
        refund_amount_paise=amount_total,
        explained_by_amount_delta_paise=delta_explained,
        explained_by_double_deduction_paise=double_explained,
    )
    # Spec §7.1: the gap between the allocated total and the exact real-valued
    # total is pure rounding, so it must stay below one paise per payment.
    residual = total - instant_total - int(exact_sum)
    return AttributeTotals(
        leakage=leakage,
        timing=timing,
        legs=legs,
        control=control,
        rounding_residual_paise=residual,
    )
