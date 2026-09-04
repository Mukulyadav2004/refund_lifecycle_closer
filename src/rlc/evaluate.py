"""Metrics against ground truth (spec §8, CLAUDE.md §8).

**This is the only module permitted to read `ground_truth.json`.** If the engine,
the loader, the invariants or the attribute calculators ever open it, the run is
grading its own homework and every number below becomes decoration.
`test_no_decision_module_reads_ground_truth` enforces that from the other side.

Denominator discipline (spec §8): every rate here is a `Ratio`, which carries its
numerator and denominator and prints them beside the percentage. A bare
percentage hides whether "94%" means 47 of 50 or 4,700 of 5,000.

No money passes through a float in this module. Rates are ratios of counts;
every paise figure is carried as an integer and formatted only at the edge.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .attributes import AttributeTotals, annotate
from .config import Config
from .engine import INFORMATIONAL_ANNOTATIONS, ClosureRun, close
from .entities import (
    CLOSED_MATCHED,
    CLOSURE_STATES,
    EXCEPTION,
    EXCEPTION_CODES,
    OPEN,
    REJECTED_INPUT,
)
from .loader import Sources

GROUND_TRUTH_FILENAME = "ground_truth.json"


# ---------------------------------------------------------------------- types


@dataclass(frozen=True, slots=True)
class Ratio:
    """A rate that refuses to be printed without its numerator and denominator."""

    numerator: int
    denominator: int

    @property
    def value(self) -> float:
        return self.numerator / self.denominator if self.denominator else 0.0

    @property
    def pct(self) -> float:
        return 100.0 * self.value

    def __str__(self) -> str:
        return f"{self.pct:6.2f}%  ({self.numerator}/{self.denominator})"

    def to_row(self) -> dict[str, Any]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "pct": round(self.pct, 4),
        }


@dataclass(frozen=True, slots=True)
class Label:
    """One row of `ground_truth.json`."""

    refund_id: str
    scenario: str
    expected_state: str
    expected_codes: tuple[str, ...]
    expected_open_reasons: tuple[str, ...]
    expected_annotations: tuple[str, ...]
    expected_timing_flags: tuple[str, ...]
    payment_id: str | None
    note: str = ""

    @classmethod
    def from_row(cls, refund_id: str, row: Mapping[str, Any]) -> "Label":
        return cls(
            refund_id=refund_id,
            scenario=row.get("scenario", "unknown"),
            expected_state=row["expected_state"],
            expected_codes=tuple(sorted(row.get("expected_codes", ()))),
            expected_open_reasons=tuple(sorted(row.get("expected_open_reasons", ()))),
            expected_annotations=tuple(sorted(row.get("expected_annotations", ()))),
            expected_timing_flags=tuple(sorted(row.get("expected_timing_flags", ()))),
            payment_id=row.get("payment_id"),
            note=row.get("note", ""),
        )


@dataclass(frozen=True, slots=True)
class CodeScore:
    """Per exception code, in the only terms that survive an unbalanced class."""

    code: str
    tp: int
    fp: int
    fn: int

    @property
    def support(self) -> int:
        return self.tp + self.fn

    @property
    def precision(self) -> Ratio:
        return Ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> Ratio:
        return Ratio(self.tp, self.tp + self.fn)

    @property
    def f1(self) -> float:
        p, r = self.precision.value, self.recall.value
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def to_row(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "support": self.support,
            "precision": self.precision.to_row(),
            "recall": self.recall.to_row(),
            "f1": round(self.f1, 4),
        }


@dataclass(frozen=True, slots=True)
class Disagreement:
    refund_id: str
    scenario: str
    field: str
    expected: tuple[str, ...] | str
    actual: tuple[str, ...] | str


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Everything spec §8 asks to be printed literally."""

    n_in: int
    state_counts: dict[str, int]
    confusion: dict[str, dict[str, int]]
    state_accuracy: Ratio
    match_rate_strict: Ratio
    match_rate_all: Ratio
    false_auto_match_rate: Ratio
    false_auto_match_rate_incl_open: Ratio
    code_scores: tuple[CodeScore, ...]
    exact_record_agreement: Ratio
    duplicate_sensitivity: dict[int, CodeScore]
    totals: AttributeTotals
    data_error_counts: dict[str, int]
    rejection_counts: dict[str, int]
    records_per_second: float
    elapsed_seconds: float
    disagreements: tuple[Disagreement, ...] = ()

    @property
    def identity_holds(self) -> bool:
        return self.n_in == sum(self.state_counts.values())

    def to_row(self) -> dict[str, Any]:
        leakage, timing, legs, control = (
            self.totals.leakage,
            self.totals.timing,
            self.totals.legs,
            self.totals.control,
        )
        return {
            "n_in": self.n_in,
            "identity_holds": self.identity_holds,
            "state_counts": self.state_counts,
            "confusion": self.confusion,
            "state_accuracy": self.state_accuracy.to_row(),
            "match_rate_strict": self.match_rate_strict.to_row(),
            "match_rate_all": self.match_rate_all.to_row(),
            "false_auto_match_rate": self.false_auto_match_rate.to_row(),
            "false_auto_match_rate_incl_open": self.false_auto_match_rate_incl_open.to_row(),
            "exact_record_agreement": self.exact_record_agreement.to_row(),
            "code_scores": [s.to_row() for s in self.code_scores],
            "duplicate_sensitivity": {
                str(window): score.to_row() for window, score in self.duplicate_sensitivity.items()
            },
            "leakage": {
                "total_paise": leakage.total_paise,
                "gst_paise": leakage.gst_paise,
                "mdr_paise": leakage.mdr_paise,
                "refunded_paise": leakage.refunded_paise,
                "leakage_bps": leakage.leakage_bps,
                "by_method": leakage.by_method,
            },
            "timing": {
                "measured": timing.measured,
                "cross_period_count": timing.cross_period_count,
                "cross_period_paise": timing.cross_period_paise,
                "late_vs_threshold_count": timing.late_vs_threshold_count,
                "lag_histogram": timing.lag_histogram,
            },
            "legs": {
                "1_initiated": legs.initiated,
                "2_gateway_processed": legs.gateway_processed,
                "3_settlement_deducted": legs.settlement_deducted,
                "4_bank_evidenced": legs.bank_evidenced,
            },
            "settlement_control": {
                "difference_paise": control.difference_paise,
                "unexplained_paise": control.unexplained_paise,
            },
            "data_errors": self.data_error_counts,
            "rejections": self.rejection_counts,
            "throughput": {
                "records_per_second": round(self.records_per_second, 1),
                "elapsed_seconds": round(self.elapsed_seconds, 6),
            },
            "disagreements": [
                {
                    "refund_id": d.refund_id,
                    "scenario": d.scenario,
                    "field": d.field,
                    "expected": list(d.expected) if isinstance(d.expected, tuple) else d.expected,
                    "actual": list(d.actual) if isinstance(d.actual, tuple) else d.actual,
                }
                for d in self.disagreements
            ],
        }


# ------------------------------------------------------------- ground truth


def load_ground_truth(path: str | Path) -> dict[str, Label]:
    """Read the labels. Only this module may call this."""
    p = Path(path)
    if p.is_dir():
        p = p / GROUND_TRUTH_FILENAME
    if not p.exists():
        raise FileNotFoundError(
            f"{p} does not exist — `make eval` scores against seeded labels, so run "
            "`make data` first. Real merchant data has no ground truth and can only "
            "be closed, not scored."
        )
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {rid: Label.from_row(rid, row) for rid, row in raw.items()}


def observed_annotations(verdict) -> tuple[str, ...]:
    """What the engine claims, in the vocabulary the generator labels with.

    Rejection reasons live in their own typed field on the verdict but arrive as
    annotations in ground truth, and `NO_RMA_MATCH` is informational — the engine
    emits it for goodwill refunds the generator never seeds, so scoring it would
    count correct behaviour as a false positive.
    """
    return tuple(
        sorted((set(verdict.annotations) - INFORMATIONAL_ANNOTATIONS) | set(verdict.rejection_reasons))
    )


# ------------------------------------------------------------------ scoring


def score_codes(
    verdicts: Mapping[str, Any], labels: Mapping[str, Label], codes: Iterable[str] = EXCEPTION_CODES
) -> tuple[CodeScore, ...]:
    scores = []
    for code in codes:
        tp = fp = fn = 0
        for refund_id, label in labels.items():
            actual = code in verdicts[refund_id].exception_codes
            expected = code in label.expected_codes
            if actual and expected:
                tp += 1
            elif actual:
                fp += 1
            elif expected:
                fn += 1
        scores.append(CodeScore(code=code, tp=tp, fp=fp, fn=fn))
    return tuple(scores)


def _confusion(verdicts, labels) -> dict[str, dict[str, int]]:
    matrix = {
        expected: {actual: 0 for actual in CLOSURE_STATES} for expected in CLOSURE_STATES
    }
    for refund_id, label in labels.items():
        matrix[label.expected_state][verdicts[refund_id].closure_state] += 1
    return matrix


def _disagreements(verdicts, labels, limit: int = 20) -> tuple[Disagreement, ...]:
    out: list[Disagreement] = []
    for refund_id, label in labels.items():
        verdict = verdicts[refund_id]
        checks = (
            ("closure_state", label.expected_state, verdict.closure_state),
            ("exception_codes", label.expected_codes, tuple(sorted(verdict.exception_codes))),
            ("open_reasons", label.expected_open_reasons, tuple(sorted(verdict.open_reasons))),
            ("annotations", label.expected_annotations, observed_annotations(verdict)),
            ("timing_flags", label.expected_timing_flags, tuple(sorted(verdict.timing_flags))),
        )
        for field_name, expected, actual in checks:
            if expected != actual:
                out.append(
                    Disagreement(
                        refund_id=refund_id,
                        scenario=label.scenario,
                        field=field_name,
                        expected=expected,
                        actual=actual,
                    )
                )
                if len(out) >= limit:
                    return tuple(out)
    return tuple(out)


def evaluate(
    run: ClosureRun,
    sources: Sources,
    cfg: Config,
    labels: Mapping[str, Label] | None = None,
    data_dir: Path | None = None,
    totals: AttributeTotals | None = None,
) -> Evaluation:
    """Score a closure run against the seeded labels (spec §8)."""
    from .loader import default_data_dir

    resolved = dict(labels) if labels is not None else load_ground_truth(
        data_dir if data_dir is not None else default_data_dir(cfg)
    )
    verdicts = run.by_id()

    missing = set(resolved) - set(verdicts)
    extra = set(verdicts) - set(resolved)
    if missing or extra:
        raise AssertionError(
            f"labels and verdicts do not cover the same refunds: {len(missing)} labelled but "
            f"not closed, {len(extra)} closed but not labelled. Zero silent drops means both "
            "sets are identical."
        )

    attribute_totals = totals if totals is not None else annotate(run, sources, cfg)
    state_counts = run.state_counts
    n_in = len(run.verdicts)

    correct_state = sum(
        1 for rid, label in resolved.items() if verdicts[rid].closure_state == label.expected_state
    )
    exact = sum(
        1
        for rid, label in resolved.items()
        if verdicts[rid].closure_state == label.expected_state
        and tuple(sorted(verdicts[rid].exception_codes)) == label.expected_codes
        and tuple(sorted(verdicts[rid].open_reasons)) == label.expected_open_reasons
        and observed_annotations(verdicts[rid]) == label.expected_annotations
        and tuple(sorted(verdicts[rid].timing_flags)) == label.expected_timing_flags
    )

    # The only number that costs real money: a seeded failure the engine waved
    # through as closed. Two denominators are reported because the manifest's
    # "seeded failures" excludes OPEN, and an OPEN refund called CLOSED_MATCHED
    # is exactly the error the maturity gate exists to prevent — it tells a
    # controller money has landed when it has not.
    seeded_failures = [
        rid for rid, label in resolved.items()
        if label.expected_state in (EXCEPTION, REJECTED_INPUT)
    ]
    not_closeable = [
        rid for rid, label in resolved.items() if label.expected_state != CLOSED_MATCHED
    ]
    waved_through = sum(
        1 for rid in seeded_failures if verdicts[rid].closure_state == CLOSED_MATCHED
    )
    waved_through_incl_open = sum(
        1 for rid in not_closeable if verdicts[rid].closure_state == CLOSED_MATCHED
    )

    sensitivity: dict[int, CodeScore] = {}
    for window in cfg.thresholds.duplicate_sensitivity_windows:
        alt = close(sources, cfg, integrity=run.integrity, duplicate_window_seconds=window)
        sensitivity[window] = score_codes(alt.by_id(), resolved, ["DUPLICATE_SUSPECT"])[0]

    return Evaluation(
        n_in=n_in,
        state_counts=state_counts,
        confusion=_confusion(verdicts, resolved),
        state_accuracy=Ratio(correct_state, n_in),
        # Excludes OPEN and REJECTED_INPUT: an open refund has not failed to
        # match, it has not finished, and a rejected one never entered.
        match_rate_strict=Ratio(
            state_counts[CLOSED_MATCHED], state_counts[CLOSED_MATCHED] + state_counts[EXCEPTION]
        ),
        match_rate_all=Ratio(state_counts[CLOSED_MATCHED], n_in),
        false_auto_match_rate=Ratio(waved_through, len(seeded_failures)),
        false_auto_match_rate_incl_open=Ratio(waved_through_incl_open, len(not_closeable)),
        code_scores=score_codes(verdicts, resolved),
        exact_record_agreement=Ratio(exact, n_in),
        duplicate_sensitivity=sensitivity,
        totals=attribute_totals,
        data_error_counts=run.integrity.channel_counts,
        rejection_counts=run.integrity.rejection_counts,
        records_per_second=run.records_per_second,
        elapsed_seconds=run.elapsed_seconds,
        disagreements=_disagreements(verdicts, resolved),
    )
