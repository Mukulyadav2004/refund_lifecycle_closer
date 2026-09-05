"""HTTP server for the live demo (stdlib only).

CLAUDE.md §10 caps runtime dependencies at `pyyaml`, so this is `http.server`
rather than a framework. For a read-mostly dashboard over a 380-record month
that is not a compromise: the whole pipeline runs in ~34 ms, so the server can
recompute the entire close on demand instead of caching stale answers.

Two design points worth stating:

* **The pipeline is the source of truth, not a JSON file.** Every endpoint reads
  an `AppState` produced by the same `loader -> invariants -> engine ->
  attributes -> explain -> evaluate` path that `make eval` runs. There is no
  second implementation of anything.
* **Payload builders are pure functions.** `summary_payload` and friends take an
  `AppState` and return a dict, so they are tested directly without binding a
  socket.

If `data/synthetic/` is missing the server generates it on startup, which is what
makes a fresh deploy self-contained: the data is deterministic from seed 42, so a
container that has never seen the repo's data produces byte-identical numbers.
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from . import attributes, explain, invariants
from .config import REPO_ROOT, Config
from .engine import ClosureRun, close
from .entities import CLOSED_MATCHED, CLOSURE_STATES, EXCEPTION, OPEN, REJECTED_INPUT
from .evaluate import Evaluation, evaluate
from .loader import Sources, default_data_dir, load_sources
from .money import format_inr
from .report import CODE_NATURE, EVIDENCE_LEG_CODES, SETTLEMENT_LEG_CODES

WEB_ROOT = REPO_ROOT / "web"
DEFAULT_PORT = 8000


# --------------------------------------------------------------------- state


@dataclass(frozen=True, slots=True)
class AppState:
    """One complete run, held in memory."""

    cfg: Config
    sources: Sources
    run: ClosureRun
    totals: attributes.AttributeTotals
    explanations: explain.ExplanationReport
    evaluation: Evaluation | None
    duplicate_window_seconds: int
    built_at: float
    build_ms: float


def ensure_data(cfg: Config, data_dir: Path | None = None) -> Path:
    """Generate the dataset if it is not on disk (a fresh container has none)."""
    directory = Path(data_dir) if data_dir else default_data_dir(cfg)
    if (directory / "refunds.json").exists():
        return directory
    from .generate import generate

    generate(cfg, directory)
    return directory


def build_state(
    cfg: Config,
    data_dir: Path | None = None,
    duplicate_window_seconds: int | None = None,
) -> AppState:
    """Run the whole pipeline. Cheap enough to redo on every parameter change."""
    started = time.perf_counter()
    directory = ensure_data(cfg, data_dir)
    sources = load_sources(cfg, directory)
    integrity = invariants.run(sources, cfg)
    window = duplicate_window_seconds or cfg.thresholds.duplicate_window_seconds
    run = close(sources, cfg, integrity=integrity, duplicate_window_seconds=window)
    totals = attributes.annotate(run, sources, cfg, integrity=integrity)

    try:
        provider = explain.build_provider(cfg)
    except RuntimeError:
        provider = None
    cap = (cfg.llm or {}).get("max_model_calls")
    explanations = explain.explain_all(
        run.verdicts, sources, cfg, provider=provider,
        max_model_calls=int(cap) if cap else None,
    )
    try:
        evaluation = evaluate(run, sources, cfg, data_dir=directory, totals=totals)
    except FileNotFoundError:
        evaluation = None  # real data has no labels; the dashboard still works

    return AppState(
        cfg=cfg,
        sources=sources,
        run=run,
        totals=totals,
        explanations=explanations,
        evaluation=evaluation,
        duplicate_window_seconds=window,
        built_at=time.time(),
        build_ms=(time.perf_counter() - started) * 1000,
    )


# ------------------------------------------------------------------ payloads


def _ratio(r) -> dict[str, Any]:
    return {"numerator": r.numerator, "denominator": r.denominator, "pct": round(r.pct, 2)}


def summary_payload(state: AppState) -> dict[str, Any]:
    run, totals, cfg = state.run, state.totals, state.cfg
    counts = run.state_counts
    leakage, timing, legs, control = (
        totals.leakage, totals.timing, totals.legs, totals.control
    )
    codes = run.code_counts
    payload: dict[str, Any] = {
        "period": {
            "start": cfg.run.period_start.isoformat(),
            "end": cfg.run.period_end.isoformat(),
            "as_of": cfg.run.as_of.isoformat(),
            "seed": cfg.run.seed,
        },
        "identity": {
            "n_in": len(run.verdicts),
            "sum": sum(counts.values()),
            "holds": len(run.verdicts) == sum(counts.values()),
        },
        "states": counts,
        "codes": codes,
        "open_reasons": run.open_reason_counts,
        "leakage": {
            "total_paise": leakage.total_paise,
            "total_inr": format_inr(leakage.total_paise),
            "gst_paise": leakage.gst_paise,
            "gst_inr": format_inr(leakage.gst_paise),
            "mdr_paise": leakage.mdr_paise,
            "mdr_inr": format_inr(leakage.mdr_paise),
            "refunded_paise": leakage.refunded_paise,
            "refunded_inr": format_inr(leakage.refunded_paise),
            "bps": leakage.leakage_bps,
            "by_method": [
                {
                    "method": method,
                    "records": b["records"],
                    "leakage_paise": b["leakage_paise"],
                    "leakage_inr": format_inr(b["leakage_paise"]),
                    "refunded_inr": format_inr(b["refunded_paise"]),
                }
                for method, b in leakage.by_method.items()
            ],
        },
        "timing": {
            "measured": timing.measured,
            "cross_period_count": timing.cross_period_count,
            "cross_period_inr": format_inr(timing.cross_period_paise),
            "late_count": timing.late_vs_threshold_count,
            "median_lag_wd": timing.median_lag_wd,
            "histogram": [{"lag": k, "count": v} for k, v in timing.lag_histogram.items()],
            "threshold_wd": cfg.thresholds.settle_threshold_wd,
        },
        "legs": [
            {"leg": 1, "name": "initiated", "status": "verified", "count": legs.initiated},
            {"leg": 2, "name": "gateway processed", "status": "verified",
             "count": legs.gateway_processed},
            {"leg": 3, "name": "settlement deducted", "status": "verified",
             "count": legs.settlement_deducted},
            {"leg": 4, "name": "bank credited", "status": "evidenced only",
             "count": legs.bank_evidenced},
        ],
        "leg_failures": {
            "settlement": sum(codes.get(c, 0) for c in SETTLEMENT_LEG_CODES),
            "evidence": sum(codes.get(c, 0) for c in EVIDENCE_LEG_CODES),
        },
        "control": {
            "difference_paise": control.difference_paise,
            "amount_delta_paise": control.explained_by_amount_delta_paise,
            "double_deducted_paise": control.explained_by_double_deduction_paise,
            "unexplained_paise": control.unexplained_paise,
        },
        "data_errors": run.integrity.channel_counts,
        "rejections": run.integrity.rejection_counts,
        "throughput": {
            "records_per_second": round(run.records_per_second),
            "close_ms": round(run.elapsed_seconds * 1000, 2),
            "pipeline_ms": round(state.build_ms, 1),
        },
        "sources": {
            "payments": len(state.sources.payments),
            "refunds": len(state.sources.refunds),
            "recon_rows": len(state.sources.recon),
            "disputes": len(state.sources.disputes),
            "settlements": len(state.sources.settlements),
            "returns_ledger": len(state.sources.returns_ledger),
        },
        "explanations": {
            "counts": state.explanations.counts,
            "provider_errors": len(state.explanations.provider_errors),
            "model_enabled": bool((cfg.llm or {}).get("enabled")),
            "model": (cfg.llm or {}).get("model"),
        },
        "duplicate_window_seconds": state.duplicate_window_seconds,
    }
    if state.evaluation is not None:
        ev = state.evaluation
        payload["rates"] = {
            "match_rate_strict": _ratio(ev.match_rate_strict),
            "match_rate_all": _ratio(ev.match_rate_all),
            "state_accuracy": _ratio(ev.state_accuracy),
            "exact_agreement": _ratio(ev.exact_record_agreement),
            "false_auto_match": _ratio(ev.false_auto_match_rate),
            "false_auto_match_incl_open": _ratio(ev.false_auto_match_rate_incl_open),
        }
    return payload


def accuracy_payload(state: AppState) -> dict[str, Any]:
    if state.evaluation is None:
        return {"available": False, "reason": "no ground truth in this dataset"}
    ev = state.evaluation
    return {
        "available": True,
        "confusion": {
            expected: [ev.confusion[expected][actual] for actual in CLOSURE_STATES]
            for expected in CLOSURE_STATES
        },
        "states": list(CLOSURE_STATES),
        "codes": [
            {
                "code": s.code,
                "nature": CODE_NATURE.get(s.code, ("", ""))[0],
                "tp": s.tp,
                "fp": s.fp,
                "fn": s.fn,
                "precision": _ratio(s.precision),
                "recall": _ratio(s.recall),
                "f1": round(s.f1, 3),
            }
            for s in ev.code_scores
        ],
        "duplicate_sensitivity": [
            {
                "window_seconds": window,
                "tp": s.tp,
                "fp": s.fp,
                "fn": s.fn,
                "precision": _ratio(s.precision),
                "recall": _ratio(s.recall),
            }
            for window, s in sorted(ev.duplicate_sensitivity.items())
        ],
        "disagreements": [
            {
                "refund_id": d.refund_id,
                "scenario": d.scenario,
                "field": d.field,
                "expected": list(d.expected) if isinstance(d.expected, tuple) else d.expected,
                "actual": list(d.actual) if isinstance(d.actual, tuple) else d.actual,
            }
            for d in ev.disagreements
        ],
    }


def _record(state: AppState, verdict) -> dict[str, Any]:
    explanation = state.explanations.explanations.get(verdict.refund_id)
    refund = state.sources.refunds_by_id[verdict.refund_id]
    parent = state.sources.parent_of(refund)
    return {
        "refund_id": verdict.refund_id,
        "payment_id": verdict.payment_id,
        "amount_paise": verdict.amount,
        "amount_inr": format_inr(verdict.amount),
        "closure_state": verdict.closure_state,
        "exception_codes": verdict.exception_codes,
        "open_reasons": verdict.open_reasons,
        "rejection_reasons": verdict.rejection_reasons,
        "annotations": verdict.annotations,
        "timing_flags": verdict.timing_flags,
        "leakage_paise": verdict.leakage_paise,
        "leakage_inr": format_inr(verdict.leakage_paise),
        "leakage_gst_inr": format_inr(verdict.leakage_gst_paise),
        "leakage_mdr_inr": format_inr(verdict.leakage_mdr_paise),
        "exposure_paise": verdict.exposure_paise,
        "exposure_inr": format_inr(verdict.exposure_paise),
        "settle_lag_wd": verdict.settle_lag_wd,
        "settlement_id": verdict.settlement_id,
        "settlement_utr": verdict.settlement_utr,
        "legs": {
            "1": verdict.leg1_initiated,
            "2": verdict.leg2_gateway_processed,
            "3": verdict.leg3_settlement_deducted,
            "4": verdict.leg4_bank_evidenced,
        },
        "confidence": verdict.confidence,
        "needs_human_review": verdict.needs_human_review,
        "evidence": verdict.evidence,
        "method": parent.method if parent else None,
        "status": refund.status,
        "receipt": refund.receipt,
        "arn": refund.arn,
        "created_at": refund.created_at,
        "explanation": explanation.text if explanation else None,
        "recommended_action": explanation.recommended_action if explanation else None,
        "explanation_source": explanation.source if explanation else None,
    }


def records_payload(
    state: AppState,
    closure_state: str | None = None,
    code: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Filtered, paginated records — sorted by exposure then leakage."""
    rows = list(state.run.verdicts)
    if closure_state:
        rows = [v for v in rows if v.closure_state == closure_state]
    if code:
        rows = [v for v in rows if code in v.exception_codes]
    if query:
        needle = query.lower()
        rows = [
            v
            for v in rows
            if needle in v.refund_id.lower() or needle in (v.payment_id or "").lower()
        ]
    rows.sort(key=lambda v: (-v.exposure_paise, -v.leakage_paise, v.refund_id))
    total = len(rows)
    page = rows[offset : offset + limit]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [_record(state, v) for v in page],
    }


def assumptions_payload(state: AppState) -> dict[str, Any]:
    t = state.cfg.thresholds
    return {
        "assumptions": state.cfg.assumptions_table(),
        "thresholds": {
            "settle_threshold_wd": t.settle_threshold_wd,
            "arn_threshold_wd": t.arn_threshold_wd,
            "pending_threshold_wd": t.pending_threshold_wd,
            "duplicate_window_seconds": t.duplicate_window_seconds,
            "duplicate_sensitivity_windows": t.duplicate_sensitivity_windows,
            "chargeback_window_days": t.chargeback_window_days,
            "amount_tolerance_paise": t.amount_tolerance_paise,
        },
        "codes": [
            {"code": code, "nature": nature, "fields": fields}
            for code, (nature, fields) in CODE_NATURE.items()
        ],
    }


# ------------------------------------------------------------------- server


class _Handler(BaseHTTPRequestHandler):
    server_version = "rlc"
    state_lock = threading.Lock()

    # Injected by `serve`.
    app_state: AppState
    cfg: Config
    data_dir: Path | None

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        pass

    # -- helpers ---------------------------------------------------------
    def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        ctype, _ = mimetypes.guess_type(str(path))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        # A redeploy must take effect on reload. The assets are a few KB, so
        # revalidating every time costs nothing and avoids demoing stale JS.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -- routing ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        def one(name: str, default: str | None = None) -> str | None:
            values = params.get(name)
            return values[0] if values else default

        if route == "/healthz":
            self._json({"ok": True, "refunds": len(type(self).app_state.run.verdicts)})
            return
        if route == "/":
            self._file(WEB_ROOT / "index.html")
            return
        if route.startswith("/static/"):
            name = route[len("/static/") :]
            if "/" in name or ".." in name:  # no traversal
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._file(WEB_ROOT / name)
            return

        state = type(self).app_state
        if route == "/api/summary":
            self._json(summary_payload(state))
        elif route == "/api/accuracy":
            self._json(accuracy_payload(state))
        elif route == "/api/assumptions":
            self._json(assumptions_payload(state))
        elif route == "/api/records":
            try:
                limit = max(1, min(500, int(one("limit", "50") or 50)))
                offset = max(0, int(one("offset", "0") or 0))
            except ValueError:
                self._json({"error": "limit and offset must be integers"},
                           HTTPStatus.BAD_REQUEST)
                return
            self._json(
                records_payload(
                    state,
                    closure_state=one("state"),
                    code=one("code"),
                    query=one("q"),
                    limit=limit,
                    offset=offset,
                )
            )
        elif route.startswith("/api/record/"):
            refund_id = route[len("/api/record/") :]
            verdict = state.run.by_id().get(refund_id)
            if verdict is None:
                self._json({"error": f"no refund {refund_id}"}, HTTPStatus.NOT_FOUND)
                return
            self._json(_record(state, verdict))
        elif route == "/api/report":
            path = state.cfg.path("out_dir") / "report.md"
            if not path.exists():
                from .report import render_report

                text = render_report(
                    state.cfg, state.run, state.sources, state.totals,
                    state.evaluation, state.explanations,
                )
            else:
                text = path.read_text(encoding="utf-8")
            self._json({"markdown": text})
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != "/api/rerun":
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "body must be JSON"}, HTTPStatus.BAD_REQUEST)
            return
        window = body.get("duplicate_window_seconds")
        if window is not None:
            try:
                window = max(60, min(30 * 86_400, int(window)))
            except (TypeError, ValueError):
                self._json({"error": "duplicate_window_seconds must be an integer"},
                           HTTPStatus.BAD_REQUEST)
                return
        cls = type(self)
        with cls.state_lock:
            cls.app_state = build_state(cls.cfg, cls.data_dir, duplicate_window_seconds=window)
        self._json(summary_payload(cls.app_state))


def create_server(
    cfg: Config,
    state: AppState,
    host: str = "127.0.0.1",
    port: int = 0,
    data_dir: Path | None = None,
) -> ThreadingHTTPServer:
    """Bind a server around an already-built state. Port 0 picks a free one."""
    _Handler.cfg = cfg
    _Handler.data_dir = data_dir
    _Handler.app_state = state
    return ThreadingHTTPServer((host, port), _Handler)


def serve(
    cfg: Config | None = None,
    host: str = "0.0.0.0",
    port: int | None = None,
    data_dir: Path | None = None,
) -> None:
    """Start the dashboard. Railway supplies the port in `$PORT`."""
    cfg = cfg or Config.load()
    resolved_port = port if port is not None else int(os.environ.get("PORT", DEFAULT_PORT))

    print("building initial state ...", flush=True)
    state = build_state(cfg, data_dir)
    httpd = create_server(cfg, state, host=host, port=resolved_port, data_dir=data_dir)
    print(
        f"ready — {len(state.run.verdicts)} refunds closed in "
        f"{state.build_ms:.0f} ms (whole pipeline)",
        flush=True,
    )
    print(f"listening on http://{host}:{resolved_port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    serve()
