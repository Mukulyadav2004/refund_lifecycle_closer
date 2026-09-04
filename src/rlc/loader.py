"""Read, normalise and index the five sources (spec §2 and §4).

Nothing in this module decides anything about a refund. Its whole job is to
turn files into entity dataclasses and to build the two join levels of spec §4:
`refund_id` binds Refunds to Recon; `payment_id` binds that result to Payments,
Disputes and the merchant returns ledger.

Two rules here carry the correctness of everything downstream.

1. This module must never read `ground_truth.json` — only `rlc.evaluate` may.
   `_refuse_ground_truth` enforces that at runtime instead of by convention,
   because the file sits in the same directory and is trivially readable.
2. The recon endpoint is keyed on SETTLEMENT date, not transaction date, so a
   refund created on 30 Aug is deducted in September. A pull window that stops
   at `period_end` manufactures false `NEVER_DEDUCTED` for every late refund
   (CLAUDE.md §12). `check_pull_window` refuses to let that pass quietly.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from .calendar_utils import BankCalendar, to_ist_date
from .config import Config
from .entities import (
    Dispute,
    Payment,
    ReconRow,
    Refund,
    ReturnsLedgerRow,
    Settlement,
)

GROUND_TRUTH_FILENAME = "ground_truth.json"

SOURCE_FILES = {
    "payments": "payments.json",
    "refunds": "refunds.json",
    "disputes": "disputes.json",
    "settlements": "settlements.json",
    "recon": "settlement_recon.json",
    "returns_ledger": "returns_ledger.csv",
}


class PullWindowError(RuntimeError):
    """The loaded window cannot support an honest closure run (spec §4)."""


class GroundTruthAccessError(RuntimeError):
    """Raised if anything but the evaluator tries to read the labels."""


def _refuse_ground_truth(path: Path) -> None:
    if path.name == GROUND_TRUTH_FILENAME:
        raise GroundTruthAccessError(
            f"{GROUND_TRUTH_FILENAME} is readable only by rlc.evaluate. The engine "
            "reading its own answer key is grading its own homework (CLAUDE.md §8)."
        )


def _read_collection(path: Path) -> list[dict[str, Any]]:
    """Read a Razorpay-shaped `{entity, count, items}` file, or a bare list."""
    _refuse_ground_truth(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    items = payload.get("items")
    if items is None:
        raise ValueError(f"{path.name}: expected a list or an object with 'items'")
    count = payload.get("count")
    if count is not None and int(count) != len(items):
        raise ValueError(
            f"{path.name}: declared count {count} != {len(items)} items — a paginated "
            "pull was truncated"
        )
    return list(items)


def _read_returns_ledger(path: Path) -> list[ReturnsLedgerRow]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [ReturnsLedgerRow.from_row(row) for row in csv.DictReader(fh)]


# ----------------------------------------------------------------- pull window


@dataclass(frozen=True, slots=True)
class PullWindow:
    """What the pull actually covers, and whether that is wide enough.

    `required_recon_end` is the first date on which a refund created on
    `period_end` could honestly be called `NEVER_DEDUCTED`: the maturity gate of
    spec §6.4. If recon coverage stops before it, every late refund in the
    period looks un-deducted when it is merely un-settled.
    """

    period_start: date
    period_end: date
    as_of: date
    required_recon_end: date
    recon_settled_first: date | None
    recon_settled_last: date | None
    payment_created_first: date | None
    refund_created_first: date | None
    refund_created_last: date | None
    refunds_missing_parent: int
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def raise_if_bad(self) -> None:
        if self.problems:
            raise PullWindowError(
                "pull window is too narrow for an honest run:\n  - "
                + "\n  - ".join(self.problems)
            )


def check_pull_window(sources: "Sources", cfg: Config, calendar: BankCalendar) -> PullWindow:
    """Compare what was pulled against what the state machine needs (spec §4)."""
    run = cfg.run
    required = calendar.add_working_days(run.period_end, cfg.thresholds.settle_threshold_wd)
    horizon = min(required, run.as_of)

    settled = [r.settled_at for r in sources.recon if r.settled_at]
    recon_first = to_ist_date(min(settled)) if settled else None
    recon_last = to_ist_date(max(settled)) if settled else None
    pay_first = (
        to_ist_date(min(p.created_at for p in sources.payments)) if sources.payments else None
    )
    ref_first = (
        to_ist_date(min(r.created_at for r in sources.refunds)) if sources.refunds else None
    )
    ref_last = (
        to_ist_date(max(r.created_at for r in sources.refunds)) if sources.refunds else None
    )
    missing_parent = sum(
        1 for r in sources.refunds if r.payment_id and r.payment_id not in sources.payments_by_id
    )

    problems: list[str] = []
    notes: list[str] = []

    if run.recon_window_end < required:
        problems.append(
            f"configured recon_window_end {run.recon_window_end} is before {required}, the "
            f"maturity gate for a refund created on {run.period_end} "
            f"(period_end + settle_threshold_wd={cfg.thresholds.settle_threshold_wd} working "
            "days). Late refunds would be called NEVER_DEDUCTED for want of a wider pull."
        )
    if not any(r.is_refund for r in sources.recon):
        problems.append("the recon pull contains no refund rows at all")
    elif recon_last is not None and recon_last < horizon:
        problems.append(
            f"recon coverage ends {recon_last} but the settlement leg cannot be judged "
            f"before {horizon}; refunds settled after {recon_last} are simply not in the pull"
        )

    if pay_first is not None and ref_first is not None and pay_first > ref_first:
        notes.append(
            f"payments start {pay_first}, after the first refund on {ref_first}; parents of "
            "early refunds may be outside the pull"
        )
    if missing_parent:
        notes.append(
            f"{missing_parent} refund(s) reference a payment that is not in the pull — "
            "each becomes NO_PARENT_PAYMENT (an integrity rejection, spec §5 I1)"
        )
    if recon_last is not None and recon_last < run.as_of:
        notes.append(f"recon coverage ends {recon_last}, {run.as_of} is as_of")

    return PullWindow(
        period_start=run.period_start,
        period_end=run.period_end,
        as_of=run.as_of,
        required_recon_end=required,
        recon_settled_first=recon_first,
        recon_settled_last=recon_last,
        payment_created_first=pay_first,
        refund_created_first=ref_first,
        refund_created_last=ref_last,
        refunds_missing_parent=missing_parent,
        problems=tuple(problems),
        notes=tuple(notes),
    )


# --------------------------------------------------------------------- sources


@dataclass(frozen=True, slots=True)
class Sources:
    """The five pulls plus the join indexes of spec §4.

    Every collection and every index is ordered deterministically, so two runs
    over the same files produce byte-identical output regardless of file order.
    """

    payments: tuple[Payment, ...]
    refunds: tuple[Refund, ...]
    disputes: tuple[Dispute, ...]
    settlements: tuple[Settlement, ...]
    recon: tuple[ReconRow, ...]
    returns_ledger: tuple[ReturnsLedgerRow, ...]

    payments_by_id: Mapping[str, Payment] = field(default_factory=dict)
    refunds_by_id: Mapping[str, Refund] = field(default_factory=dict)
    refunds_by_payment: Mapping[str, tuple[Refund, ...]] = field(default_factory=dict)
    recon_refunds_by_refund_id: Mapping[str, tuple[ReconRow, ...]] = field(default_factory=dict)
    recon_payment_by_payment_id: Mapping[str, ReconRow] = field(default_factory=dict)
    recon_by_settlement: Mapping[str, tuple[ReconRow, ...]] = field(default_factory=dict)
    settlements_by_id: Mapping[str, Settlement] = field(default_factory=dict)
    disputes_by_payment: Mapping[str, tuple[Dispute, ...]] = field(default_factory=dict)
    rma_by_payment: Mapping[str, tuple[ReturnsLedgerRow, ...]] = field(default_factory=dict)

    # ------------------------------------------------------------- accessors

    def parent_of(self, refund: Refund) -> Payment | None:
        """The parent payment, or None when the refund has no reachable parent."""
        if not refund.payment_id:
            return None
        return self.payments_by_id.get(refund.payment_id)

    def settlement_rows(self, refund_id: str) -> tuple[ReconRow, ...]:
        """S(r) of spec §6.1 — the recon refund rows claiming this refund."""
        return self.recon_refunds_by_refund_id.get(refund_id, ())

    def sibling_refunds(self, payment_id: str | None) -> tuple[Refund, ...]:
        """R(P) of spec §6.1 — non-failed refunds of the parent payment.

        Failed refunds move no money, so they are excluded from ΣR, from
        duplicate pairs and from leakage allocation (CLAUDE.md §12).
        """
        if not payment_id:
            return ()
        return tuple(
            r for r in self.refunds_by_payment.get(payment_id, ()) if r.counts_toward_parent_total
        )

    def disputes_of(self, payment_id: str | None) -> tuple[Dispute, ...]:
        if not payment_id:
            return ()
        return self.disputes_by_payment.get(payment_id, ())

    def rmas_of(self, payment_id: str | None) -> tuple[ReturnsLedgerRow, ...]:
        if not payment_id:
            return ()
        return self.rma_by_payment.get(payment_id, ())

    # ------------------------------------------------------------------ build

    @classmethod
    def build(
        cls,
        payments: Iterable[Payment],
        refunds: Iterable[Refund],
        disputes: Iterable[Dispute],
        settlements: Iterable[Settlement],
        recon: Iterable[ReconRow],
        returns_ledger: Iterable[ReturnsLedgerRow],
    ) -> "Sources":
        pay = tuple(sorted(payments, key=lambda p: (p.created_at, p.id)))
        ref = tuple(sorted(refunds, key=lambda r: (r.created_at, r.id)))
        dis = tuple(sorted(disputes, key=lambda d: (d.created_at, d.id)))
        stl = tuple(sorted(settlements, key=lambda s: (s.created_at, s.id)))
        rec = tuple(sorted(recon, key=lambda r: (r.settled_at, r.type, r.entity_id)))
        rma = tuple(sorted(returns_ledger, key=lambda r: (r.rma_created_at, r.rma_id)))

        by_payment: dict[str, list[Refund]] = defaultdict(list)
        for r in ref:
            if r.payment_id:
                by_payment[r.payment_id].append(r)

        recon_refunds: dict[str, list[ReconRow]] = defaultdict(list)
        recon_payment: dict[str, ReconRow] = {}
        by_settlement: dict[str, list[ReconRow]] = defaultdict(list)
        for row in rec:
            if row.settlement_id:
                by_settlement[row.settlement_id].append(row)
            if row.is_refund:
                recon_refunds[row.entity_id].append(row)
            elif row.is_payment:
                recon_payment.setdefault(row.entity_id, row)

        disputes_by_payment: dict[str, list[Dispute]] = defaultdict(list)
        for d in dis:
            disputes_by_payment[d.payment_id].append(d)

        rma_by_payment: dict[str, list[ReturnsLedgerRow]] = defaultdict(list)
        for row in rma:
            rma_by_payment[row.payment_id].append(row)

        return cls(
            payments=pay,
            refunds=ref,
            disputes=dis,
            settlements=stl,
            recon=rec,
            returns_ledger=rma,
            payments_by_id={p.id: p for p in pay},
            refunds_by_id={r.id: r for r in ref},
            refunds_by_payment={k: tuple(v) for k, v in by_payment.items()},
            recon_refunds_by_refund_id={k: tuple(v) for k, v in recon_refunds.items()},
            recon_payment_by_payment_id=recon_payment,
            recon_by_settlement={k: tuple(v) for k, v in by_settlement.items()},
            settlements_by_id={s.id: s for s in stl},
            disputes_by_payment={k: tuple(v) for k, v in disputes_by_payment.items()},
            rma_by_payment={k: tuple(v) for k, v in rma_by_payment.items()},
        )


def default_data_dir(cfg: Config) -> Path:
    """Where `make data` writes and `make close` reads."""
    return cfg.path("data_dir") / "synthetic"


def load_sources(cfg: Config, data_dir: Path | None = None, strict: bool = True) -> Sources:
    """Load the five pulls from `data_dir` and verify the window (spec §4).

    `strict=False` returns the sources without raising, for tests that want to
    observe what a too-narrow pull actually does to the numbers.
    """
    directory = Path(data_dir) if data_dir else default_data_dir(cfg)
    if not directory.is_dir():
        raise FileNotFoundError(f"{directory} does not exist — run `make data` first")

    sources = Sources.build(
        payments=[Payment.from_api(d) for d in _read_collection(directory / SOURCE_FILES["payments"])],
        refunds=[Refund.from_api(d) for d in _read_collection(directory / SOURCE_FILES["refunds"])],
        disputes=[Dispute.from_api(d) for d in _read_collection(directory / SOURCE_FILES["disputes"])],
        settlements=[
            Settlement.from_api(d) for d in _read_collection(directory / SOURCE_FILES["settlements"])
        ],
        recon=[ReconRow.from_api(d) for d in _read_collection(directory / SOURCE_FILES["recon"])],
        returns_ledger=_read_returns_ledger(directory / SOURCE_FILES["returns_ledger"]),
    )
    if strict:
        check_pull_window(sources, cfg, cfg.calendar()).raise_if_bad()
    return sources
