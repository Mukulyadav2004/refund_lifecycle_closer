"""Typed loader for config.yaml (spec §1, CLAUDE.md §4).

Every threshold in this project is an assumption, not a documented constant.
Keeping them here means the report can print them verbatim and the README can
list them honestly. `Config.assumptions_table()` produces exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .calendar_utils import BankCalendar

REPO_ROOT = Path(__file__).resolve().parents[2]


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


@dataclass(frozen=True)
class RunConfig:
    seed: int
    timezone: str
    as_of: date
    period_start: date
    period_end: date
    payments_window_start: date
    recon_window_end: date


@dataclass(frozen=True)
class PricingConfig:
    fee_bps: int
    gst_bps: int
    instant_refund_fee_paise: int
    enable_instant_refunds: bool


@dataclass(frozen=True)
class SettlementConfig:
    cycle_wd: int
    international_cycle_wd: int
    refund_deduction_lag_wd: int
    refund_deduction_lag_tail: list[int]
    refund_deduction_lag_tail_prob: float


@dataclass(frozen=True)
class ThresholdConfig:
    settle_threshold_wd: int
    arn_threshold_wd: int
    pending_threshold_wd: int
    amount_tolerance_paise: int
    duplicate_window_seconds: int
    duplicate_sensitivity_windows: list[int]
    chargeback_window_days: int


@dataclass(frozen=True)
class Config:
    run: RunConfig
    pricing: PricingConfig
    settlement: SettlementConfig
    thresholds: ThresholdConfig
    generator: dict[str, Any]
    paths: dict[str, str]
    llm: dict[str, Any]
    root: Path = field(default=REPO_ROOT)

    # ------------------------------------------------------------------ load

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        root = REPO_ROOT
        cfg_path = Path(path) if path else root / "config.yaml"
        raw = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))
        return cls.from_dict(raw, root=Path(cfg_path).resolve().parent)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], root: Path = REPO_ROOT) -> "Config":
        r, p = raw["run"], raw["pricing"]
        s, t = raw["settlement"], raw["thresholds"]
        cfg = cls(
            run=RunConfig(
                seed=int(r["seed"]),
                timezone=r.get("timezone", "Asia/Kolkata"),
                as_of=_as_date(r["as_of"]),
                period_start=_as_date(r["period_start"]),
                period_end=_as_date(r["period_end"]),
                payments_window_start=_as_date(r["payments_window_start"]),
                recon_window_end=_as_date(r["recon_window_end"]),
            ),
            pricing=PricingConfig(
                fee_bps=int(p["fee_bps"]),
                gst_bps=int(p["gst_bps"]),
                instant_refund_fee_paise=int(p.get("instant_refund_fee_paise", 0)),
                enable_instant_refunds=bool(p.get("enable_instant_refunds", False)),
            ),
            settlement=SettlementConfig(
                cycle_wd=int(s["cycle_wd"]),
                international_cycle_wd=int(s.get("international_cycle_wd", 7)),
                refund_deduction_lag_wd=int(s["refund_deduction_lag_wd"]),
                refund_deduction_lag_tail=list(s.get("refund_deduction_lag_tail", [2, 3, 4])),
                refund_deduction_lag_tail_prob=float(s.get("refund_deduction_lag_tail_prob", 0.1)),
            ),
            thresholds=ThresholdConfig(
                settle_threshold_wd=int(t["settle_threshold_wd"]),
                arn_threshold_wd=int(t["arn_threshold_wd"]),
                pending_threshold_wd=int(t["pending_threshold_wd"]),
                amount_tolerance_paise=int(t.get("amount_tolerance_paise", 0)),
                duplicate_window_seconds=int(t["duplicate_window_seconds"]),
                duplicate_sensitivity_windows=list(t.get("duplicate_sensitivity_windows", [])),
                chargeback_window_days=int(t["chargeback_window_days"]),
            ),
            generator=raw.get("generator", {}),
            paths=raw.get("paths", {}),
            llm=raw.get("llm", {}),
            root=root,
        )
        cfg.validate()
        return cfg

    # -------------------------------------------------------------- validate

    def validate(self) -> None:
        r = self.run
        if r.period_start > r.period_end:
            raise ValueError("period_start must not be after period_end")
        if r.payments_window_start > r.period_start:
            raise ValueError(
                "payments_window_start must precede period_start, or parent payments "
                "fall outside the pull window and the engine invents NO_PARENT_PAYMENT"
            )
        if r.recon_window_end < r.period_end:
            raise ValueError(
                "recon_window_end must be at least period_end: the recon endpoint is "
                "keyed on settlement date, so late-August refunds settle in September. "
                "A narrow window manufactures false NEVER_DEDUCTED."
            )
        if r.as_of < r.period_end:
            raise ValueError("as_of must be on or after period_end")
        if self.thresholds.settle_threshold_wd < self.settlement.cycle_wd:
            raise ValueError(
                "settle_threshold_wd must be >= settlement cycle, otherwise the "
                "maturity gate fires before a refund could possibly have settled"
            )
        if not 0 <= self.settlement.refund_deduction_lag_tail_prob <= 1:
            raise ValueError("refund_deduction_lag_tail_prob must be a probability")

    # --------------------------------------------------------------- helpers

    def path(self, key: str) -> Path:
        value = self.paths.get(key)
        if value is None:
            raise KeyError(f"no path configured for {key!r}")
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    def calendar(self) -> BankCalendar:
        return BankCalendar.from_json(self.path("holidays"))

    def assumptions_table(self) -> list[dict[str, str]]:
        """Rows for the README and report: what is assumed, and why."""
        return [
            {"key": "fee_bps", "value": str(self.pricing.fee_bps),
             "note": "ASSUMPTION: public base pricing; real merchants negotiate"},
            {"key": "gst_bps", "value": str(self.pricing.gst_bps),
             "note": "18% GST on the fee; Payment.fee is GST-inclusive"},
            {"key": "settlement.cycle_wd", "value": str(self.settlement.cycle_wd),
             "note": "DOCUMENTED: domestic T+2 working days"},
            {"key": "settlement.refund_deduction_lag_wd", "value": str(self.settlement.refund_deduction_lag_wd),
             "note": "ASSUMPTION: Razorpay's recon sample nets a refund next-day; "
                     "'T+5 to T+7' is marketing copy and is NOT hardcoded"},
            {"key": "thresholds.settle_threshold_wd", "value": str(self.thresholds.settle_threshold_wd),
             "note": "ASSUMPTION: maturity gate before any NEVER_DEDUCTED call"},
            {"key": "thresholds.arn_threshold_wd", "value": str(self.thresholds.arn_threshold_wd),
             "note": "ASSUMPTION: no documented ARN SLA exists"},
            {"key": "thresholds.pending_threshold_wd", "value": str(self.thresholds.pending_threshold_wd),
             "note": "ASSUMPTION: no documented SLA exists"},
            {"key": "thresholds.duplicate_window_seconds", "value": str(self.thresholds.duplicate_window_seconds),
             "note": "ASSUMPTION: the one heuristic rule; sensitivity is reported"},
            {"key": "thresholds.chargeback_window_days", "value": str(self.thresholds.chargeback_window_days),
             "note": "Card-network dispute window, industry figure"},
            {"key": "run.timezone", "value": self.run.timezone,
             "note": "Razorpay timestamps are Unix UTC; all periods are IST"},
        ]
