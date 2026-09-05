"""Tests for the output artifacts (SPEC.md §10).

The report is the deliverable a judge actually reads, so these tests check the
two disciplines that are easy to lose in a long document: every percentage keeps
its numerator and denominator, and the vocabulary the project banned stays
banned. The rest checks that each artifact contains what it claims to.
"""

from __future__ import annotations

import csv
import json
import re

import pytest

from rlc import attributes, engine, evaluate as ev, explain, report
from rlc.entities import EXCEPTION


@pytest.fixture(scope="module")
def artifacts(sources, cfg, dataset, tmp_path_factory):
    run = engine.close(sources, cfg)
    totals = attributes.annotate(run, sources, cfg)
    explanations = explain.explain_all(run.verdicts, sources, cfg)
    labels = {rid: ev.Label.from_row(rid, row) for rid, row in dataset.ground_truth.items()}
    evaluation = ev.evaluate(run, sources, cfg, labels=labels, totals=totals)
    log = report.RunLog()
    log.stage("load", refunds=len(sources.refunds))
    log.stage("close", **run.state_counts)
    out = tmp_path_factory.mktemp("out")
    written = {
        "results": report.write_results_jsonl(
            out / "results.jsonl", run, explanations.explanations
        ),
        "exceptions": report.write_exceptions_csv(
            out / "exceptions.csv", run, explanations.explanations
        ),
        "report": report.write_report_md(
            out / "report.md", cfg, run, sources, totals, evaluation, explanations
        ),
        "run_log": report.write_run_log(out / "run.log", log),
    }
    return written, run, evaluation, totals


@pytest.fixture(scope="module")
def report_text(artifacts):
    return artifacts[0]["report"].read_text(encoding="utf-8")


# ------------------------------------------------------------ results.jsonl


def test_results_has_one_line_per_refund(artifacts):
    written, run, _evaluation, _totals = artifacts
    lines = written["results"].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(run.verdicts)
    rows = [json.loads(line) for line in lines]
    assert {r["refund_id"] for r in rows} == {v.refund_id for v in run.verdicts}


def test_results_carries_state_leakage_timing_legs_and_explanation(artifacts):
    written, _run, _evaluation, _totals = artifacts
    row = json.loads(written["results"].read_text(encoding="utf-8").splitlines()[0])
    for key in (
        "closure_state",
        "exception_codes",
        "open_reasons",
        "leakage_paise",
        "leakage_gst_paise",
        "leakage_mdr_paise",
        "timing_flags",
        "legs",
        "evidence",
        "explanation",
    ):
        assert key in row, key


# ----------------------------------------------------------- exceptions.csv


def test_exceptions_csv_holds_every_exception(artifacts):
    written, run, _evaluation, _totals = artifacts
    with written["exceptions"].open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    expected = [v for v in run.verdicts if v.closure_state == EXCEPTION]
    assert len(rows) == len(expected)
    assert {r["refund_id"] for r in rows} == {v.refund_id for v in expected}


def test_exceptions_csv_is_sorted_by_exposure(artifacts):
    """The list a controller works from, most expensive first."""
    written, run, _evaluation, _totals = artifacts
    with written["exceptions"].open(encoding="utf-8") as fh:
        order = [r["refund_id"] for r in csv.DictReader(fh)]
    by_id = run.by_id()
    exposures = [by_id[rid].exposure_paise for rid in order]
    assert exposures == sorted(exposures, reverse=True)


def test_the_heuristic_rule_is_marked_as_such_in_the_csv(artifacts):
    written, _run, _evaluation, _totals = artifacts
    with written["exceptions"].open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    duplicates = [r for r in rows if "DUPLICATE_SUSPECT" in r["exception_codes"]]
    assert duplicates
    for row in duplicates:
        assert "HEURISTIC" in row["nature"]
        assert row["needs_human_review"] == "True"
        assert row["confidence"]


# ------------------------------------------------------------------ run.log


def test_the_run_log_records_every_stage_with_counts(artifacts):
    written, _run, _evaluation, _totals = artifacts
    text = written["run_log"].read_text(encoding="utf-8")
    assert "load" in text and "close" in text
    assert "refunds=" in text
    assert "CLOSED_MATCHED=" in text
    assert "TOTAL" in text


# ---------------------------------------------------------------- report.md


def test_the_report_states_the_claim_precisely(report_text):
    """CLAUDE.md §1: never "4-leg lifecycle"; always 3 verified, 1 evidenced."""
    assert "Three legs verified, one leg evidenced" in report_text
    assert "evidenced only" in report_text.lower()


@pytest.mark.parametrize("banned", ["4-leg", "four-leg", "orphan", "all four legs"])
def test_the_report_never_uses_banned_vocabulary(report_text, banned):
    assert banned.lower() not in report_text.lower()


def test_every_percentage_carries_its_denominator(report_text):
    """CLAUDE.md §8: no bare percentages.

    Matches only the two-decimal form `_pct` produces, so the "18% GST" in the
    assumptions note — which is a stated rate, not a computed one — is not swept
    up by this.
    """
    bare = []
    for match in re.finditer(r"\d+\.\d{2}%", report_text):
        following = report_text[match.end() : match.end() + 24]
        if not re.match(r"\s*\(\d+/\d+\)", following):
            bare.append(report_text[max(0, match.start() - 40) : match.end() + 24])
    assert not bare, f"percentages printed without a denominator: {bare[:3]}"


def test_the_identity_equation_is_printed_and_holds(report_text, artifacts):
    _written, run, _evaluation, _totals = artifacts
    counts = run.state_counts
    assert f"N_in {len(run.verdicts)} ==" in report_text
    assert f"{counts['CLOSED_MATCHED']} CLOSED_MATCHED" in report_text
    assert "HOLDS" in report_text
    assert "UNEXPLAINED" in report_text
    assert "(must be 0)" in report_text


def test_evidence_failures_are_reported_apart_from_settlement_failures(report_text):
    """CLAUDE.md §5: ARN_OVERDUE is counted separately and never summed in."""
    assert "settlement leg (money did not move as expected)" in report_text
    assert "evidence leg (no bank reference)" in report_text
    assert "never summed" in report_text


def test_the_only_heuristic_rule_says_so(report_text):
    """CLAUDE.md §6 asks for this statement explicitly."""
    assert "only non-arithmetic rule" in report_text
    assert "Duplicate window sensitivity" in report_text


def test_the_lag_is_a_distribution_not_an_assertion(report_text):
    assert "Settlement lag histogram" in report_text
    assert "rather than asserted" in report_text


def test_leakage_is_explained_as_fee_not_fee_plus_tax(report_text):
    assert "never `fee + tax`" in report_text
    assert "leakage_bps" in report_text


def test_every_assumption_is_printed_as_an_assumption(report_text, cfg):
    """CLAUDE.md §0.2: assumptions are never restated as fact."""
    assert "## Assumptions" in report_text
    for row in cfg.assumptions_table():
        assert f"`{row['key']}`" in report_text
    assert "unless the note says DOCUMENTED" in report_text


def test_the_report_says_what_the_score_does_not_show(report_text):
    """A perfect score against your own generator needs its caveat attached."""
    assert "What these numbers do and do not show" in report_text
    assert "internal consistency" in report_text
    assert "not accuracy against a real merchant" in report_text


def test_the_report_records_where_the_model_sits(report_text):
    assert "## AI judgment" in report_text
    assert "No model touches classification" in report_text


def test_throughput_is_reported(report_text):
    assert "records/second" in report_text


# --------------------------------------------------- no ground truth needed


def test_the_report_renders_without_an_evaluation(sources, cfg):
    """Real merchant data has no labels; the run must still produce a report."""
    run = engine.close(sources, cfg)
    totals = attributes.annotate(run, sources, cfg)
    text = report.render_report(cfg, run, sources, totals, evaluation=None)
    assert "Identity equations" in text
    assert "## Leakage" in text
    assert "## Assumptions" in text
    assert "Match rates" not in text
    assert "confusion matrix" not in text.lower()


def test_write_all_produces_every_artifact(sources, cfg, tmp_path, monkeypatch):
    import dataclasses

    scoped = dataclasses.replace(cfg, paths={**cfg.paths, "out_dir": str(tmp_path)})
    run = engine.close(sources, scoped)
    totals = attributes.annotate(run, sources, scoped)
    written = report.write_all(scoped, run, sources, totals, log=report.RunLog())
    assert set(written) == {"results", "exceptions", "report", "run_log"}
    for path in written.values():
        assert path.exists() and path.stat().st_size > 0
