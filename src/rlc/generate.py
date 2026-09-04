"""Synthetic data generator (SPEC.md §3).

Writes six files shaped exactly like the documented Razorpay API responses, plus
a ground-truth label file that only the evaluator is allowed to read.

Order matters and follows the spec:

    Step 0  config, calendar, rng
    Step 1  payments
    Step 2  returns ledger (what each return was actually worth)
    Step 3  refunds, happy path
    Step 4  seeded scenarios, layered on top
    Step 5  settlements and recon rows, derived from the seeded directives
    Step 6  ground truth and manifest

Two structural rules the generator must not break, because the engine's
credibility depends on them:

* A refund never pushes its parent past the captured amount. Razorpay returns a
  400 for that, so it cannot occur in real data. Over-refunds are seeded as
  "more than the return was worth", not "more than the payment".
* Recon rows are emitted from per-refund directives set in step 4, never
  inferred in step 5. That is how NEVER_DEDUCTED omits a row, DOUBLE_DEDUCTED
  emits two, and SETTLEMENT_AMOUNT_DELTA perturbs the debit.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from .calendar_utils import BankCalendar, ist_unix, month_key, to_ist_date
from .config import Config
from .entities import (
    CLOSED_MATCHED,
    EXCEPTION,
    OPEN,
    REJECTED_INPUT,
    Dispute,
    Payment,
    ReconRow,
    Refund,
    ReturnsLedgerRow,
    Settlement,
)
from .ids import IdFactory
from .money import fee_breakdown

BUSINESS_HOURS = (9, 21)

CARD_NETWORKS = ("Visa", "MasterCard", "RuPay")
CARD_ISSUERS = ("HDFC", "ICIC", "SBIN", "KARB", "AXIS")


# --------------------------------------------------------------------- plans


@dataclass
class RefundPlan:
    """A refund plus its recon directives and the label the evaluator will grade.

    `emit_recon`, `recon_rows` and `debit_delta` are what step 5 reads. Nothing
    in step 5 decides on its own whether a refund settled.
    """

    refund: Refund
    order_id: str | None
    method: str
    scenario: str = "happy_path"
    expected_state: str = CLOSED_MATCHED
    expected_codes: list[str] = field(default_factory=list)
    expected_open: list[str] = field(default_factory=list)
    expected_annotations: list[str] = field(default_factory=list)
    expected_timing: list[str] = field(default_factory=list)
    emit_recon: bool = True
    recon_rows: int = 1
    debit_delta: int = 0
    lag_wd: int | None = None
    rma_id: str | None = None
    note: str = ""

    @property
    def created_date(self) -> date:
        return to_ist_date(self.refund.created_at)


@dataclass
class Dataset:
    payments: list[Payment]
    refunds: list[Refund]
    disputes: list[Dispute]
    settlements: list[Settlement]
    recon: list[ReconRow]
    returns_ledger: list[ReturnsLedgerRow]
    ground_truth: dict[str, dict[str, Any]]
    manifest: dict[str, Any]

    def write(self, out_dir: Path) -> dict[str, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}

        def collection(items: Iterable[Any]) -> dict[str, Any]:
            rows = [i.to_api() for i in items]
            return {"entity": "collection", "count": len(rows), "items": rows}

        for name, items in (
            ("payments", self.payments),
            ("refunds", self.refunds),
            ("disputes", self.disputes),
            ("settlements", self.settlements),
            ("settlement_recon", self.recon),
        ):
            path = out_dir / f"{name}.json"
            path.write_text(json.dumps(collection(items), indent=2), encoding="utf-8")
            written[name] = path

        ledger_path = out_dir / "returns_ledger.csv"
        with ledger_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["rma_id", "order_id", "payment_id",
                            "expected_refund_paise", "rma_created_at", "reason"],
            )
            writer.writeheader()
            for row in self.returns_ledger:
                writer.writerow(row.to_row())
        written["returns_ledger"] = ledger_path

        gt_path = out_dir / "ground_truth.json"
        gt_path.write_text(json.dumps(self.ground_truth, indent=2), encoding="utf-8")
        written["ground_truth"] = gt_path

        mf_path = out_dir / "manifest.json"
        mf_path.write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")
        written["manifest"] = mf_path
        return written


# ----------------------------------------------------------------- generator


class SyntheticDataGenerator:
    """Deterministic generator. Same seed, same bytes."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cal: BankCalendar = cfg.calendar()
        self.rng = random.Random(cfg.run.seed)
        self.ids = IdFactory(self.rng)

        self.as_of: date = cfg.run.as_of
        self.period_start: date = cfg.run.period_start
        self.period_end: date = cfg.run.period_end
        self.pay_start: date = cfg.run.payments_window_start

        self.payments: list[Payment] = []
        self.by_id: dict[str, Payment] = {}
        self.ledger: list[ReturnsLedgerRow] = []
        self.plans: list[RefundPlan] = []
        self.disputes: list[Dispute] = []
        self.counts: dict[str, int] = {}
        self._available: list[RefundPlan] = []

    # ---------------------------------------------------------------- build

    def build(self) -> Dataset:
        self._step1_payments()
        self._step2_returns_ledger()
        self._step3_refunds_happy_path()
        self._step4_seed_scenarios()
        self._recompute_parent_totals()
        recon, settlements = self._step5_settlements_and_recon()
        self._label_immature(recon)
        self._label_timing(recon)
        ground_truth = self._step6_ground_truth()
        manifest = self._manifest(recon, settlements)
        self._self_check(recon, settlements)
        return Dataset(
            payments=self.payments,
            refunds=[p.refund for p in self.plans],
            disputes=self.disputes,
            settlements=settlements,
            recon=recon,
            returns_ledger=self.ledger,
            ground_truth=ground_truth,
            manifest=manifest,
        )

    # ------------------------------------------------------------- step 1

    def _step1_payments(self) -> None:
        g = self.cfg.generator
        n = int(g.get("n_payments", 600))
        mix = g.get("method_mix", {"upi": 0.55, "card": 0.30, "netbanking": 0.10, "wallet": 0.05})
        methods = list(mix)
        weights = [mix[m] for m in methods]

        for _ in range(n):
            # Payments run to `as_of`, not `period_end`: the merchant keeps
            # trading after the reporting month closes. Stopping at period_end
            # left September settlement days holding refund debits with no
            # payment credits, which netted negative — a payout can never be.
            created = self._random_ts(self.pay_start, self.as_of)
            amount = self._random_amount()
            method = self.rng.choices(methods, weights=weights, k=1)[0]
            fee, _fee_ex, tax = fee_breakdown(
                amount, self.cfg.pricing.fee_bps, self.cfg.pricing.gst_bps
            )
            is_card = method == "card"
            p = Payment(
                id=self.ids.payment(),
                amount=amount,
                status="captured",
                method=method,
                order_id=self.ids.order(),
                fee=fee,
                tax=tax,
                created_at=created,
                card_network=self.rng.choice(CARD_NETWORKS) if is_card else None,
                card_type=self.rng.choice(("credit", "debit")) if is_card else None,
                card_issuer=self.rng.choice(CARD_ISSUERS) if is_card else None,
            )
            self.payments.append(p)
            self.by_id[p.id] = p

        self.payments.sort(key=lambda p: p.created_at)

    # ------------------------------------------------------------- step 2

    def _step2_returns_ledger(self) -> None:
        """One RMA per refund event. This is the merchant's own truth.

        5% of chosen payments get two partial RMAs. Those become legitimate
        multi-partial refunds, and they are the population the duplicate rule
        must not false-positive on.
        """
        g = self.cfg.generator
        n_events = int(g.get("n_refund_events", 340))
        full_share = float(g.get("full_refund_share", 0.70))

        cutoff = ist_unix(self.period_end, 23)
        eligible = [p for p in self.payments if p.created_at <= cutoff]
        chosen = self.rng.sample(eligible, min(n_events, len(eligible)))

        seq = 0
        for p in chosen:
            if self.rng.random() < 0.05:
                amounts = [
                    self._round_rupees(int(p.amount * self.rng.uniform(0.15, 0.30))),
                    self._round_rupees(int(p.amount * self.rng.uniform(0.15, 0.30))),
                ]
            elif self.rng.random() < full_share:
                amounts = [p.amount]
            else:
                amounts = [self._round_rupees(int(p.amount * self.rng.uniform(0.10, 0.60)))]

            for amt in amounts:
                if amt <= 0:
                    continue
                seq += 1
                self.ledger.append(
                    ReturnsLedgerRow(
                        rma_id=f"RMA-{seq:05d}",
                        order_id=p.order_id,
                        payment_id=p.id,
                        expected_refund_paise=amt,
                        rma_created_at=self._clamp_ts(
                            p.created_at + self.rng.randint(1, 25) * 86400,
                            lo=p.created_at + 3600,
                            hi=ist_unix(self.as_of, 20),
                        ),
                        reason=self.rng.choice(
                            ("customer_return", "order_cancelled", "item_damaged", "size_issue")
                        ),
                    )
                )

    # ------------------------------------------------------------- step 3

    def _step3_refunds_happy_path(self) -> None:
        receipt_prob = float(self.cfg.generator.get("receipt_present_prob", 0.70))
        for rma in self.ledger:
            p = self.by_id[rma.payment_id]
            refund = Refund(
                id=self.ids.refund(),
                payment_id=p.id,
                amount=rma.expected_refund_paise,
                status="processed",
                receipt=rma.rma_id if self.rng.random() < receipt_prob else None,
                arn=None,
                created_at=self._clamp_ts(
                    rma.rma_created_at + self.rng.randint(0, 2) * 86400,
                    lo=rma.rma_created_at,
                    hi=ist_unix(self.as_of, 20),
                ),
                speed_requested="normal",
                speed_processed="normal",
            )
            self._set_arn(refund)
            self.plans.append(
                RefundPlan(refund=refund, order_id=p.order_id, method=p.method, rma_id=rma.rma_id)
            )

    def _set_arn(self, refund: Refund, force_null: bool = False) -> None:
        """The ARN arrives after the refund is marked processed, not with it."""
        if force_null or refund.status != "processed":
            refund.arn = None
            return
        lag = self.rng.randint(0, int(self.cfg.generator.get("arn_lag_wd_max", 5)))
        arrives = self.cal.add_working_days(to_ist_date(refund.created_at), lag)
        refund.arn = self.ids.arn() if arrives <= self.as_of else None

    # ------------------------------------------------------------- step 4

    def _step4_seed_scenarios(self) -> None:
        sc = self.cfg.generator.get("scenarios", {})
        self._available = list(self.plans)
        self.rng.shuffle(self._available)

        self._seed_amount_mismatch_over(int(sc.get("amount_mismatch_over", 5)))
        self._seed_amount_mismatch_under(int(sc.get("amount_mismatch_under", 3)))
        self._seed_never_deducted(int(sc.get("never_deducted", 7)))
        self._seed_double_deducted(int(sc.get("double_deducted", 3)))
        self._seed_settlement_delta(int(sc.get("settlement_amount_delta", 4)))
        self._seed_arn_overdue(int(sc.get("arn_overdue", 8)))
        self._seed_cross_period(10)
        self._seed_duplicates(int(sc.get("duplicate_true", 10)))
        self._seed_duplicate_hard_negatives(int(sc.get("duplicate_hard_negative", 6)))
        self._seed_failed_superseded(int(sc.get("refund_failed_superseded", 2)))
        self._seed_failed_not_superseded(int(sc.get("refund_failed_not_superseded", 2)))
        self._seed_pending_overdue(int(sc.get("pending_overdue", 2)))
        self._seed_open_awaiting(int(sc.get("open_awaiting", 15)))
        self._seed_pending_young(int(sc.get("pending_young", 3)))
        self._seed_disputes(sc)
        self._seed_rejected_inputs(int(sc.get("rejected_input", 3)))

    # -- pool helpers -------------------------------------------------------

    def _take(self, n: int, predicate=None) -> list[RefundPlan]:
        """Claim up to `n` untouched happy-path plans matching `predicate`."""
        taken: list[RefundPlan] = []
        for plan in list(self._available):
            if len(taken) >= n:
                break
            if plan.scenario != "happy_path":
                continue
            if predicate and not predicate(plan):
                continue
            taken.append(plan)
            self._available.remove(plan)
        return taken

    def _age_wd(self, plan: RefundPlan) -> int:
        return self.cal.working_days_between(plan.created_date, self.as_of)

    def _sibling_total(self, payment_id: str, exclude_id: str) -> int:
        return sum(
            p.refund.amount
            for p in self.plans
            if p.refund.payment_id == payment_id
            and p.refund.id != exclude_id
            and p.refund.status != "failed"
            and p.expected_state != REJECTED_INPUT
        )

    def _headroom(self, plan: RefundPlan) -> int:
        """How much more this parent could absorb without breaching ΣR ≤ captured."""
        p = self.by_id[plan.refund.payment_id]
        return p.amount - self._sibling_total(p.id, plan.refund.id) - plan.refund.amount

    def _mark(self, plan: RefundPlan, scenario: str, state: str,
              codes: list[str] | None = None, open_reasons: list[str] | None = None,
              annotations: list[str] | None = None, note: str = "") -> None:
        plan.scenario = scenario
        plan.expected_state = state
        plan.expected_codes = list(codes or [])
        plan.expected_open = list(open_reasons or [])
        plan.expected_annotations = list(annotations or [])
        plan.note = note
        self.counts[scenario] = self.counts.get(scenario, 0) + 1

    # -- scenarios ----------------------------------------------------------

    def _seed_amount_mismatch_over(self, n: int) -> None:
        """Refunded far more than the return was worth.

        Note this is NOT "more than the payment" — Razorpay blocks that with a
        400. The overshoot is capped at the remaining headroom, so the data stays
        something Razorpay could actually have produced. Catching it requires the
        merchant's returns ledger, which is the honest version of this claim.
        """
        eligible = lambda p: p.refund.amount * 3 <= self.by_id[p.refund.payment_id].amount
        for plan in self._take(n, eligible):
            expected = plan.refund.amount
            inflated = min(expected * 10, expected + self._headroom(plan))
            inflated = self._round_rupees(inflated)
            if inflated <= expected:
                continue
            plan.refund.amount = inflated
            self._mark(plan, "amount_mismatch_over", EXCEPTION, ["AMOUNT_MISMATCH"],
                       note=f"return worth {expected} paise, refunded {inflated}")

    def _seed_amount_mismatch_under(self, n: int) -> None:
        for plan in self._take(n, lambda p: p.refund.amount > 60_000):
            expected = plan.refund.amount
            plan.refund.amount = expected - self.rng.randint(500, 5000)
            self._mark(plan, "amount_mismatch_under", EXCEPTION, ["AMOUNT_MISMATCH"],
                       note=f"return worth {expected} paise, refunded {plan.refund.amount}")

    def _seed_never_deducted(self, n: int) -> None:
        """Processed, matured, and the settlement deduction never arrived."""
        for plan in self._take(n, lambda p: self._age_wd(p) >= 5):
            plan.emit_recon = False
            self._mark(plan, "never_deducted", EXCEPTION, ["NEVER_DEDUCTED"],
                       note="processed and matured, no recon row emitted")

    def _seed_double_deducted(self, n: int) -> None:
        for plan in self._take(n, lambda p: self._age_wd(p) >= 5):
            plan.recon_rows = 2
            self._mark(plan, "double_deducted", EXCEPTION, ["DOUBLE_DEDUCTED"],
                       note="deducted twice, on consecutive settlement dates")

    def _seed_settlement_delta(self, n: int) -> None:
        for plan in self._take(n, lambda p: self._age_wd(p) >= 5 and p.refund.amount > 3000):
            delta = self.rng.choice((1, -1)) * self.rng.randint(100, 2000)
            plan.debit_delta = delta
            self._mark(plan, "settlement_amount_delta", EXCEPTION, ["SETTLEMENT_AMOUNT_DELTA"],
                       note=f"recon debit differs from the refund amount by {delta} paise")

    def _seed_arn_overdue(self, n: int) -> None:
        """Marked processed long ago, still no bank reference number.

        This is the leg-4 gap made concrete: Razorpay says processed, and the
        merchant has no evidence the customer's bank ever saw it.
        """
        for plan in self._take(n, lambda p: self._age_wd(p) >= 12):
            plan.refund.arn = None
            self._mark(plan, "arn_overdue", EXCEPTION, ["ARN_OVERDUE"],
                       note="processed, no ARN well past the threshold")

    def _seed_cross_period(self, n: int) -> None:
        """An August refund whose deduction lands in September's settlement.

        Not an error, a timing artifact, so the expected state stays matched and
        only a timing flag is recorded.
        """
        late = [d for d in (self.period_end - timedelta(days=i) for i in range(5))
                if self.cal.is_working_day(d)]
        if not late:
            return
        for plan in self._take(n, lambda p: self._age_wd(p) >= 2):
            created_d = self.rng.choice(late)
            lag = self._lag_that_crosses_month(created_d)
            if lag is None:
                continue
            plan.refund.created_at = ist_unix(created_d, self.rng.randint(*BUSINESS_HOURS))
            plan.lag_wd = lag
            self._set_arn(plan.refund)
            plan.expected_timing = ["CROSS_PERIOD"]
            self._mark(plan, "cross_period", CLOSED_MATCHED,
                       note="deducted correctly, just in the next month's batch")

    def _lag_that_crosses_month(self, created_d: date) -> int | None:
        for lag in range(1, 5):
            settled = self.cal.add_working_days(created_d, lag)
            if settled > self.as_of:
                return None
            if (settled.year, settled.month) != (created_d.year, created_d.month):
                return lag
        return None

    def _seed_duplicates(self, n: int) -> None:
        """A second identical refund minutes later, with no receipt to guard it."""
        eligible = lambda p: self._headroom(p) >= p.refund.amount and self._age_wd(p) >= 5
        for original in self._take(n, eligible):
            clone = Refund(
                id=self.ids.refund(),
                payment_id=original.refund.payment_id,
                amount=original.refund.amount,
                status="processed",
                receipt=None,  # no idempotency key, so Razorpay accepted it
                created_at=original.refund.created_at + self.rng.randint(1, 90) * 60,
                speed_requested="normal",
                speed_processed="normal",
            )
            self._set_arn(clone)
            clone_plan = RefundPlan(refund=clone, order_id=original.order_id,
                                    method=original.method, rma_id=original.rma_id)
            self.plans.append(clone_plan)
            self._mark(original, "duplicate_original", CLOSED_MATCHED,
                       note="the legitimate first refund of a duplicated pair")
            self._mark(clone_plan, "duplicate_clone", EXCEPTION, ["DUPLICATE_SUSPECT"],
                       note="same amount, minutes later, no receipt on either side")

    def _seed_duplicate_hard_negatives(self, n: int) -> None:
        """Two identical refunds a day apart, both with distinct receipts.

        Razorpay accepted both, so they are legitimate separate refunds. The
        engine must not flag them. This population is what keeps the reported
        duplicate precision honest instead of flattering.
        """
        eligible = lambda p: self._headroom(p) >= p.refund.amount and self._age_wd(p) >= 5
        for first in self._take(n, eligible):
            first.refund.receipt = f"{first.rma_id or 'RMA'}-A"
            second = Refund(
                id=self.ids.refund(),
                payment_id=first.refund.payment_id,
                amount=first.refund.amount,
                status="processed",
                receipt=f"{first.rma_id or 'RMA'}-B",
                created_at=first.refund.created_at + 86400,
                speed_requested="normal",
                speed_processed="normal",
            )
            self._set_arn(second)
            second_plan = RefundPlan(refund=second, order_id=first.order_id,
                                     method=first.method, rma_id=first.rma_id)
            self.plans.append(second_plan)
            self._mark(first, "duplicate_hard_negative_a", CLOSED_MATCHED,
                       note="distinct receipts, a legitimate split refund")
            self._mark(second_plan, "duplicate_hard_negative_b", CLOSED_MATCHED,
                       note="distinct receipts, a legitimate split refund")

    def _seed_failed_superseded(self, n: int) -> None:
        """A failed refund that was successfully re-issued. Not an exception."""
        eligible = lambda p: self._headroom(p) >= p.refund.amount and self._age_wd(p) >= 6
        for plan in self._take(n, eligible):
            plan.refund.status = "failed"
            plan.refund.arn = None
            plan.emit_recon = False  # a failed refund moves no money
            retry = Refund(
                id=self.ids.refund(),
                payment_id=plan.refund.payment_id,
                amount=plan.refund.amount,
                status="processed",
                receipt=None,
                created_at=plan.refund.created_at + self.rng.randint(1, 3) * 86400,
                speed_requested="normal",
                speed_processed="normal",
            )
            self._set_arn(retry)
            retry_plan = RefundPlan(refund=retry, order_id=plan.order_id,
                                    method=plan.method, rma_id=plan.rma_id)
            self.plans.append(retry_plan)
            self._mark(plan, "refund_failed_superseded", CLOSED_MATCHED,
                       annotations=["FAILED_SUPERSEDED"], note="failed, then re-issued")
            self._mark(retry_plan, "refund_failed_retry", CLOSED_MATCHED,
                       note="the successful re-issue")

    def _seed_failed_not_superseded(self, n: int) -> None:
        for plan in self._take(n, lambda p: self._age_wd(p) >= 6):
            plan.refund.status = "failed"
            plan.refund.arn = None
            plan.emit_recon = False
            self._mark(plan, "refund_failed", EXCEPTION, ["REFUND_FAILED"],
                       note="failed and never re-issued; the customer still has no money")

    def _seed_pending_overdue(self, n: int) -> None:
        for plan in self._take(n, lambda p: self._age_wd(p) >= 12):
            plan.refund.status = "pending"
            plan.refund.arn = None
            plan.emit_recon = False
            self._mark(plan, "pending_overdue", EXCEPTION, ["PENDING_OVERDUE"],
                       note="stuck in pending well past the threshold")

    def _seed_open_awaiting(self, n: int) -> None:
        """Young refunds that simply have not settled yet. Correct behaviour.

        Without the maturity gate these become false NEVER_DEDUCTED calls, so
        this population is what proves the gate works.
        """
        young = self._young_working_days()
        if not young:
            return
        for plan in self._take(n):
            plan.refund.created_at = ist_unix(self.rng.choice(young), self.rng.randint(*BUSINESS_HOURS))
            plan.refund.status = "processed"
            plan.refund.arn = None
            plan.emit_recon = False
            self._mark(plan, "open_awaiting", OPEN, [],
                       ["AWAITING_SETTLEMENT", "AWAITING_ARN"],
                       note="created in the last day or two; nothing is wrong yet")

    def _seed_pending_young(self, n: int) -> None:
        young = self._young_working_days()
        if not young:
            return
        for plan in self._take(n):
            plan.refund.created_at = ist_unix(self.rng.choice(young), self.rng.randint(*BUSINESS_HOURS))
            plan.refund.status = "pending"
            plan.refund.arn = None
            plan.emit_recon = False
            self._mark(plan, "pending_young", OPEN, [], ["AWAITING_PROCESSING"],
                       note="Razorpay is still attempting the refund")

    def _young_working_days(self) -> list[date]:
        """Dates recent enough that the maturity gate has not yet expired.

        The gate fires when `now >= settle_due`, so only dates strictly inside
        the window qualify.
        """
        out: list[date] = []
        d = self.as_of
        for _ in range(10):
            d -= timedelta(days=1)
            if not self.cal.is_working_day(d):
                continue
            due = self.cal.add_working_days(d, self.cfg.thresholds.settle_threshold_wd)
            if self.as_of < due:
                out.append(d)
        return out

    def _seed_disputes(self, sc: dict[str, Any]) -> None:
        """Chargebacks, in the only ordering Razorpay actually permits.

        A refund cannot be created while a dispute is under investigation
        (documented 400), so chargeback-then-refund is unreachable. It is seeded
        as a dispute with no refund at all, which is the honest version.
        """
        window_s = self.cfg.thresholds.chargeback_window_days * 86400
        as_of_ts = ist_unix(self.as_of, 12)

        def dispute_after(plan: RefundPlan, status: str, deducted_full: bool) -> Dispute | None:
            p = self.by_id[plan.refund.payment_id]
            created = min(plan.refund.created_at + self.rng.randint(5, 40) * 86400, as_of_ts)
            if created <= plan.refund.created_at or (created - p.created_at) > window_s:
                return None
            return Dispute(
                id=self.ids.dispute(),
                payment_id=p.id,
                amount=p.amount,
                amount_deducted=p.amount if deducted_full else 0,
                reason_code="chargeback",
                reason_description="Customer disputed the transaction",
                respond_by=created + 7 * 86400,
                status=status,
                phase="chargeback",
                created_at=created,
                evidence={"refund_confirmation": None, "submitted_at": None},
            )

        mature = lambda p: self._age_wd(p) >= 8

        for plan in self._take(int(sc.get("refund_plus_chargeback_realized", 3)), mature):
            d = dispute_after(plan, "lost", True)
            if d is None:
                continue
            self.disputes.append(d)
            self._mark(plan, "refund_plus_chargeback_realized", EXCEPTION,
                       ["REFUND_PLUS_CHARGEBACK"],
                       note="refund paid, then the chargeback was lost: paid out twice")

        for plan in self._take(int(sc.get("refund_plus_chargeback_at_risk", 2)), mature):
            d = dispute_after(plan, "under_review", False)
            if d is None:
                continue
            self.disputes.append(d)
            self._mark(plan, "refund_plus_chargeback_at_risk", EXCEPTION,
                       ["REFUND_PLUS_CHARGEBACK"],
                       note="refund paid, chargeback still under review: exposure at risk")

        for plan in self._take(int(sc.get("dispute_won_after_refund", 1)), mature):
            d = dispute_after(plan, "won", False)
            if d is None:
                continue
            self.disputes.append(d)
            self._mark(plan, "dispute_won_after_refund", CLOSED_MATCHED,
                       annotations=["DISPUTE_RESOLVED"],
                       note="chargeback won, so there is no double payout")

        refunded = {p.refund.payment_id for p in self.plans}
        clean = [p for p in self.payments if p.id not in refunded]
        self.rng.shuffle(clean)
        wanted = int(sc.get("chargeback_first_no_refund", 2)) + int(sc.get("disputes_unrelated", 5))
        made = 0
        for p in clean:
            if made >= wanted:
                break
            created = min(p.created_at + self.rng.randint(5, 60) * 86400, as_of_ts)
            if (created - p.created_at) > window_s or created <= p.created_at:
                continue
            self.disputes.append(
                Dispute(
                    id=self.ids.dispute(),
                    payment_id=p.id,
                    amount=p.amount,
                    amount_deducted=0,
                    reason_code="chargeback",
                    reason_description="Customer disputed the transaction",
                    respond_by=created + 7 * 86400,
                    status=self.rng.choice(("open", "under_review")),
                    phase="chargeback",
                    created_at=created,
                    evidence={"refund_confirmation": None, "submitted_at": None},
                )
            )
            made += 1
        self.counts["dispute_without_refund"] = made

    def _seed_rejected_inputs(self, n: int) -> None:
        """Malformed records. Integrity failures, not merchant exceptions.

        They are counted, reported, and excluded from the match-rate denominator
        with the exclusion stated.
        """
        parent = self.rng.choice(self.payments)
        specs = [
            ("negative_amount", "NEGATIVE_AMOUNT"),
            ("missing_payment_id", "NO_PARENT_PAYMENT"),
            ("refund_before_payment", "REFUND_BEFORE_PAYMENT"),
        ][:n]

        for kind, reason in specs:
            refund = Refund(
                id=self.ids.refund(),
                payment_id=parent.id,
                amount=25_000,
                status="processed",
                receipt=None,
                arn=None,
                created_at=parent.created_at + 86400,
            )
            if kind == "negative_amount":
                refund.amount = -500
            elif kind == "missing_payment_id":
                refund.payment_id = None
            elif kind == "refund_before_payment":
                refund.created_at = parent.created_at - 3 * 86400

            plan = RefundPlan(refund=refund, order_id=parent.order_id,
                              method=parent.method, emit_recon=False)
            self.plans.append(plan)
            self._mark(plan, f"rejected_{kind}", REJECTED_INPUT, note=reason)
            plan.expected_annotations = [reason]

    # ------------------------------------------------------- parent totals

    def _recompute_parent_totals(self) -> None:
        """amount_refunded and refund_status must agree with the refunds file.

        Failed refunds move no money and are excluded, and so are the malformed
        REJECTED_INPUT records, which do not represent real money either.
        """
        totals: dict[str, int] = {}
        for plan in self.plans:
            r = plan.refund
            if r.payment_id is None or r.amount <= 0 or plan.expected_state == REJECTED_INPUT:
                continue
            if r.status == "failed":
                continue
            totals[r.payment_id] = totals.get(r.payment_id, 0) + r.amount

        for p in self.payments:
            total = totals.get(p.id, 0)
            if total > p.amount:
                raise AssertionError(
                    f"{p.id}: refunds total {total} paise exceed the captured {p.amount}. "
                    "Razorpay returns a 400 for this, so the generator must never emit it."
                )
            p.amount_refunded = total
            if total == 0:
                p.refund_status = None
            elif total == p.amount:
                p.refund_status = "full"
                p.status = "refunded"
            else:
                p.refund_status = "partial"

    # ------------------------------------------------------------- step 5

    def _step5_settlements_and_recon(self) -> tuple[list[ReconRow], list[Settlement]]:
        rows: list[ReconRow] = []

        for p in self.payments:
            settled_d = self.cal.add_working_days(to_ist_date(p.created_at),
                                                  self.cfg.settlement.cycle_wd)
            if settled_d > self.as_of:
                continue
            rows.append(
                ReconRow(
                    entity_id=p.id,
                    type="payment",
                    debit=0,
                    credit=p.amount - p.fee,
                    amount=p.amount,
                    fee=p.fee,
                    tax=p.tax,
                    created_at=p.created_at,
                    settled_at=ist_unix(settled_d, 6),
                    payment_id=None,          # null on payment rows
                    order_id=p.order_id,
                    method=p.method,
                    card_network=p.card_network,
                    card_issuer=p.card_issuer,
                    card_type=p.card_type,
                    description="Payment settled",
                )
            )

        dispute_by_payment = {d.payment_id: d.id for d in self.disputes}

        for plan in self.plans:
            r = plan.refund
            if not plan.emit_recon or r.status != "processed" or r.payment_id is None:
                continue
            created_d = to_ist_date(r.created_at)
            lag = (
                plan.lag_wd
                if plan.lag_wd is not None
                else self._effective_lag(created_d, self._sample_lag())
            )
            for i in range(plan.recon_rows):
                settled_d = self.cal.add_working_days(created_d, lag + i)
                if settled_d > self.as_of:
                    continue
                rows.append(
                    ReconRow(
                        entity_id=r.id,
                        type="refund",
                        debit=r.amount + (plan.debit_delta if i == 0 else 0),
                        credit=0,
                        amount=r.amount,
                        fee=0,   # refund rows never carry a fee; that is the whole point
                        tax=0,
                        created_at=r.created_at,
                        settled_at=ist_unix(settled_d, 6),
                        payment_id=r.payment_id,
                        order_id=plan.order_id,
                        method=plan.method,
                        dispute_id=dispute_by_payment.get(r.payment_id),
                    )
                )

        settlements = self._group_into_settlements(rows)
        rows.sort(key=lambda r: (r.settled_at, r.type, r.entity_id))
        return rows, settlements

    def _sample_lag(self) -> int:
        s = self.cfg.settlement
        if self.rng.random() < s.refund_deduction_lag_tail_prob and s.refund_deduction_lag_tail:
            return self.rng.choice(s.refund_deduction_lag_tail)
        return s.refund_deduction_lag_wd

    def _group_into_settlements(self, rows: list[ReconRow]) -> list[Settlement]:
        by_day: dict[date, list[ReconRow]] = {}
        for row in rows:
            by_day.setdefault(to_ist_date(row.settled_at), []).append(row)

        settlements: list[Settlement] = []
        for day in sorted(by_day):
            day_rows = by_day[day]
            sid = self.ids.settlement()
            utr = self.ids.utr(day.strftime("%Y%m%d"))
            net = sum(r.net for r in day_rows)
            if net < 0:
                raise AssertionError(
                    f"settlement on {day} nets {net} paise. A payout is never negative; "
                    "raise generator.n_payments or lower n_refund_events."
                )
            for row in day_rows:
                row.settlement_id = sid
                row.settlement_utr = utr
            settlements.append(
                Settlement(id=sid, amount=net, status="processed", fees=0, tax=0,
                           utr=utr, created_at=ist_unix(day, 6))
            )
        return settlements

    def _effective_lag(self, created_d: date, lag: int) -> int:
        """Pull a sampled deduction lag back inside the observation window.

        A refund whose sampled lag settles after `as_of` is in flight, which is
        honest only while the maturity gate has not passed. Past the gate the
        same record is indistinguishable from NEVER_DEDUCTED, and the generator
        would be writing a label no engine could earn. When that happens, settle
        it at the latest lag that still lands on or before `as_of`.

        This is the tension between `refund_deduction_lag_tail` (up to 4 working
        days) and `settle_threshold_wd` (3): the tail can outrun the gate. Both
        are assumptions, so neither is "wrong" — but the data must not sit in the
        gap between them.
        """
        if self.cal.add_working_days(created_d, lag) <= self.as_of:
            return lag
        gate = self.cal.add_working_days(created_d, self.cfg.thresholds.settle_threshold_wd)
        if self.as_of < gate:
            return lag  # genuinely young, and provably so
        for smaller in range(lag - 1, 0, -1):
            if self.cal.add_working_days(created_d, smaller) <= self.as_of:
                return smaller
        return lag

    # ----------------------------------------------------- step 5b: maturity

    def _label_immature(self, recon: list[ReconRow]) -> None:
        """Label refunds that had not matured by `as_of` (spec §6.4, §6.7).

        Step 5 skips a recon row whose settlement date falls after `as_of`, and
        `_set_arn` leaves `arn` null when the ARN would arrive after `as_of`.
        Both correctly model an in-flight refund — but the plan still carried the
        default `CLOSED_MATCHED`, which claims three verified legs for a refund
        that has none. The label has to follow the facts the generator actually
        emitted, not the intent it started with.

        This reads only the generator's own output. It does not re-implement the
        state machine: "did I write a recon row for this refund" and "did I give
        it an ARN" are facts about this file, and the two assertions below fail
        loudly if a threshold change ever makes them ambiguous.
        """
        emitted: dict[str, int] = {}
        for row in recon:
            if row.is_refund:
                emitted[row.entity_id] = emitted.get(row.entity_id, 0) + 1

        for plan in self.plans:
            r = plan.refund
            if r.status != "processed":
                continue  # failed and pending refunds are labelled by their own scenarios
            if plan.expected_state == REJECTED_INPUT:
                continue  # stage 0 stops before the settlement and evidence legs

            reasons: list[str] = []
            if plan.emit_recon and emitted.get(r.id, 0) == 0:
                settle_due = self.cal.add_working_days(
                    plan.created_date, self.cfg.thresholds.settle_threshold_wd
                )
                assert self.as_of < settle_due, (
                    f"{r.id}: no recon row was emitted, yet the maturity gate {settle_due} "
                    f"has already passed as_of {self.as_of}. The record is indistinguishable "
                    "from NEVER_DEDUCTED, so the label would be a coin toss. Raise "
                    "settle_threshold_wd or shorten refund_deduction_lag_tail."
                )
                reasons.append("AWAITING_SETTLEMENT")

            if r.arn is None and "ARN_OVERDUE" not in plan.expected_codes:
                arn_due = self.cal.add_working_days(
                    plan.created_date, self.cfg.thresholds.arn_threshold_wd
                )
                assert self.as_of < arn_due, (
                    f"{r.id}: no ARN, and arn_due {arn_due} has already passed as_of "
                    f"{self.as_of}. generator.arn_lag_wd_max must stay below "
                    "thresholds.arn_threshold_wd, or unseeded refunds drift into ARN_OVERDUE."
                )
                reasons.append("AWAITING_ARN")

            if not reasons:
                continue
            for reason in reasons:
                if reason not in plan.expected_open:
                    plan.expected_open.append(reason)
            # Stage 6 precedence: codes outrank open reasons, so a seeded
            # exception stays an exception and only gains the open reasons.
            if plan.expected_state == CLOSED_MATCHED and not plan.expected_codes:
                plan.expected_state = OPEN
                if plan.scenario == "happy_path":
                    plan.scenario = "open_immature"
                    self.counts["open_immature"] = self.counts.get("open_immature", 0) + 1

    def _label_timing(self, recon: list[ReconRow]) -> None:
        """Derive timing flags from the settlement dates actually written (spec §7.2).

        Timing is an attribute of the data, not a seeded scenario. Only 10
        refunds were *steered* across a month boundary, but any refund created
        near month end and deducted in the next batch crosses one too, and the
        labels have to describe the file rather than the intent — the same
        mistake as `_label_immature` fixed for `expected_state`.

        This reads the `settled_at` values it just wrote. What the evaluator
        scores with it is therefore whether the engine joins the recon rows and
        converts to IST correctly, which is the exact failure mode CLAUDE.md §12
        opens with: a refund created 31 Aug 23:30 IST is 30 Aug in UTC, and a
        UTC month test silently reports the wrong month. The 10 deliberately
        seeded cases remain identifiable by their `cross_period` scenario.
        """
        rows_by_refund: dict[str, list[ReconRow]] = {}
        for row in recon:
            if row.is_refund:
                rows_by_refund.setdefault(row.entity_id, []).append(row)

        for plan in self.plans:
            if plan.expected_state == REJECTED_INPUT:
                continue  # stage 0 stops before any attribute is computed
            rows = rows_by_refund.get(plan.refund.id, [])
            if len(rows) != 1:
                # Timing needs an unambiguous settlement date, so a
                # double-deducted refund has none (spec §7.2).
                plan.expected_timing = []
                continue
            created_d = plan.created_date
            settled_d = to_ist_date(rows[0].settled_at)
            flags: list[str] = []
            if month_key(created_d) != month_key(settled_d):
                flags.append("CROSS_PERIOD")
            lag = self.cal.working_days_between(created_d, settled_d)
            if lag > self.cfg.thresholds.settle_threshold_wd:
                flags.append("LATE_VS_THRESHOLD")
            if plan.scenario == "cross_period":
                assert "CROSS_PERIOD" in flags, (
                    f"{plan.refund.id}: seeded as cross_period but settled {settled_d} "
                    f"in the same month it was created ({created_d})"
                )
            plan.expected_timing = flags

    # ------------------------------------------------------------- step 6

    def _step6_ground_truth(self) -> dict[str, dict[str, Any]]:
        return {
            plan.refund.id: {
                "scenario": plan.scenario,
                "expected_state": plan.expected_state,
                "expected_codes": sorted(plan.expected_codes),
                "expected_open_reasons": sorted(plan.expected_open),
                "expected_annotations": sorted(plan.expected_annotations),
                "expected_timing_flags": sorted(plan.expected_timing),
                "payment_id": plan.refund.payment_id,
                "note": plan.note,
            }
            for plan in self.plans
        }

    def _manifest(self, recon: list[ReconRow], settlements: list[Settlement]) -> dict[str, Any]:
        states: dict[str, int] = {}
        for plan in self.plans:
            states[plan.expected_state] = states.get(plan.expected_state, 0) + 1
        seeded_failures = sum(
            1 for p in self.plans if p.expected_state in (EXCEPTION, REJECTED_INPUT)
        )
        return {
            "seed": self.cfg.run.seed,
            "as_of": self.as_of.isoformat(),
            "period": [self.period_start.isoformat(), self.period_end.isoformat()],
            "payments_window_start": self.pay_start.isoformat(),
            "counts": {
                "payments": len(self.payments),
                "refunds": len(self.plans),
                "returns_ledger": len(self.ledger),
                "disputes": len(self.disputes),
                "settlements": len(settlements),
                "recon_rows": len(recon),
                "recon_refund_rows": sum(1 for r in recon if r.is_refund),
                "seeded_failures": seeded_failures,
            },
            "expected_states": states,
            "seeded_scenarios": dict(sorted(self.counts.items())),
            "assumptions": self.cfg.assumptions_table(),
        }

    # -------------------------------------------------------- sanity checks

    def _self_check(self, recon: list[ReconRow], settlements: list[Settlement]) -> None:
        """Fail loudly rather than ship data the engine cannot fairly be graded on."""
        ids = [p.refund.id for p in self.plans]
        assert len(ids) == len(set(ids)), "duplicate refund ids generated"

        for plan in self.plans:
            r = plan.refund
            if plan.expected_state == REJECTED_INPUT:
                continue
            assert r.amount > 0, f"{r.id}: non-positive amount outside a rejected seed"
            assert r.payment_id in self.by_id, f"{r.id}: parent missing from the pull window"
            parent = self.by_id[r.payment_id]
            assert r.created_at >= parent.created_at, f"{r.id}: refund predates its payment"
            assert r.created_at <= ist_unix(self.as_of, 23, 59), f"{r.id}: created after as_of"

        refund_rows: dict[str, list[ReconRow]] = {}
        for row in recon:
            assert row.settlement_id, "recon row left without a settlement id"
            if row.is_refund:
                refund_rows.setdefault(row.entity_id, []).append(row)
            else:
                assert row.payment_id is None, "payment rows carry a null payment_id"
                assert row.credit == row.amount - row.fee, "payment credit must be amount - fee"

        for row in recon:
            if row.is_refund:
                assert row.fee == 0 and row.tax == 0, "refund rows never carry a fee"
                assert row.credit == 0, "refund rows are debits"

        for plan in self.plans:
            rows = refund_rows.get(plan.refund.id, [])
            if plan.scenario == "never_deducted":
                assert not rows, f"{plan.refund.id}: NEVER_DEDUCTED seed still emitted a row"
            if plan.scenario == "double_deducted":
                assert len(rows) == 2, f"{plan.refund.id}: expected 2 rows, got {len(rows)}"
            # OPEN is not a synonym for "unsettled": legs 3 and 4 are
            # independent, so a refund can be deducted from settlement and still
            # be waiting on its ARN. Only the settlement reason implies no row.
            if "AWAITING_SETTLEMENT" in plan.expected_open:
                assert not rows, (
                    f"{plan.refund.id}: AWAITING_SETTLEMENT seed still emitted a recon row"
                )
            if plan.refund.status == "failed":
                assert not rows, f"{plan.refund.id}: a failed refund moves no money"

        by_settlement: dict[str, int] = {}
        for row in recon:
            by_settlement[row.settlement_id] = by_settlement.get(row.settlement_id, 0) + row.net
        for s in settlements:
            assert s.amount >= 0, f"{s.id}: negative settlement"
            assert by_settlement.get(s.id, 0) == s.amount, (
                f"{s.id}: control total does not tie "
                f"({by_settlement.get(s.id)} vs {s.amount})"
            )

    # ------------------------------------------------------------ utilities

    def _random_amount(self) -> int:
        """Ticket size between ₹200 and ₹50,000, in whole rupees."""
        rupees = int(self.rng.lognormvariate(7.0, 0.9))
        return max(200, min(50_000, rupees)) * 100

    @staticmethod
    def _round_rupees(paise: int) -> int:
        return (paise // 100) * 100

    def _random_ts(self, lo: date, hi: date) -> int:
        d = lo + timedelta(days=self.rng.randint(0, max((hi - lo).days, 0)))
        return ist_unix(d, self.rng.randint(*BUSINESS_HOURS), self.rng.randint(0, 59))

    @staticmethod
    def _clamp_ts(ts: int, lo: int, hi: int) -> int:
        return max(lo, min(ts, hi))


# ---------------------------------------------------------------- entrypoint


def generate(cfg: Config | None = None, out_dir: Path | None = None) -> Dataset:
    cfg = cfg or Config.load()
    dataset = SyntheticDataGenerator(cfg).build()
    target = out_dir or (cfg.root / "data" / "synthetic")
    written = dataset.write(target)
    print_summary(dataset, written)
    return dataset


def print_summary(dataset: Dataset, written: dict[str, Path]) -> None:
    m = dataset.manifest
    print(f"seed {m['seed']}   as_of {m['as_of']}   period {m['period'][0]} .. {m['period'][1]}")
    print("\ncounts")
    for k, v in m["counts"].items():
        print(f"  {k:<22} {v}")
    print("\nexpected closure states")
    for k, v in sorted(m["expected_states"].items()):
        print(f"  {k:<22} {v}")
    print("\nseeded scenarios")
    for k, v in m["seeded_scenarios"].items():
        print(f"  {k:<34} {v}")
    print("\nwritten")
    for name, path in written.items():
        print(f"  {name:<18} {path}")


if __name__ == "__main__":  # pragma: no cover
    generate()
