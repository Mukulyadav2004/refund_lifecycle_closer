"""Tests for the evaluator (SPEC.md §8, CLAUDE.md §8).

The evaluator is the only module allowed to read `ground_truth.json`, so it is
also the only place a scoring bug can hide behind a good-looking number. These
tests do two things: check that a perfect run scores perfectly, and — more
usefully — check that the metrics actually *move* when the engine is wrong.
A metric that cannot go down measures nothing.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from rlc import attributes, engine, evaluate as ev
from rlc.entities import CLOSED_MATCHED, EXCEPTION, OPEN, REJECTED_INPUT, RefundVerdict


@pytest.fixture(scope="module")
def scored(sources, cfg, dataset):
    run = engine.close(sources, cfg)
    totals = attributes.annotate(run, sources, cfg)
    labels = {
        rid: ev.Label.from_row(rid, row) for rid, row in dataset.ground_truth.items()
    }
    return ev.evaluate(run, sources, cfg, labels=labels, totals=totals), run, labels


def _replace_verdict(run, refund_id, **changes):
    """A copy of the run with one verdict altered, for negative controls."""
    verdicts = tuple(
        dataclasses.replace(v, **changes) if v.refund_id == refund_id else v
        for v in run.verdicts
    )
    return dataclasses.replace(run, verdicts=verdicts)


# ---------------------------------------------------------------------- ratio


def test_a_ratio_always_carries_its_denominator():
    """Spec §8 denominator discipline: no bare percentages anywhere."""
    ratio = ev.Ratio(47, 50)
    assert "47/50" in str(ratio)
    assert ratio.pct == pytest.approx(94.0)
    assert ratio.to_row() == {"numerator": 47, "denominator": 50, "pct": 94.0}


def test_a_ratio_with_no_denominator_is_zero_not_an_error():
    assert ev.Ratio(0, 0).value == 0.0
    assert "0/0" in str(ev.Ratio(0, 0))


# ------------------------------------------------------------- perfect run


def test_the_identity_equation_is_reported_and_holds(scored):
    result, _run, _labels = scored
    assert result.identity_holds
    assert result.n_in == sum(result.state_counts.values())


def test_the_engine_agrees_with_every_label(scored):
    result, _run, _labels = scored
    assert result.state_accuracy.numerator == result.n_in
    assert result.exact_record_agreement.numerator == result.n_in
    assert result.disagreements == ()


def test_every_code_is_scored_even_at_zero_support(scored):
    result, _run, _labels = scored
    assert len(result.code_scores) == 9
    for score in result.code_scores:
        assert score.fp == 0 and score.fn == 0
        assert score.f1 == pytest.approx(1.0)


def test_no_seeded_failure_is_waved_through(scored):
    """The only number that costs real money."""
    result, _run, _labels = scored
    assert result.false_auto_match_rate.numerator == 0
    assert result.false_auto_match_rate.denominator > 0
    assert result.false_auto_match_rate_incl_open.numerator == 0


def test_the_two_false_auto_match_denominators_differ(scored):
    """The manifest's "seeded failures" excludes OPEN; the inclusive one does not."""
    result, _run, labels = scored
    seeded = sum(
        1 for l in labels.values() if l.expected_state in (EXCEPTION, REJECTED_INPUT)
    )
    not_closeable = sum(1 for l in labels.values() if l.expected_state != CLOSED_MATCHED)
    assert result.false_auto_match_rate.denominator == seeded
    assert result.false_auto_match_rate_incl_open.denominator == not_closeable
    assert not_closeable > seeded


def test_match_rate_strict_excludes_open_and_rejected(scored):
    result, _run, _labels = scored
    counts = result.state_counts
    assert result.match_rate_strict.denominator == counts[CLOSED_MATCHED] + counts[EXCEPTION]
    assert result.match_rate_all.denominator == result.n_in
    assert result.match_rate_strict.value >= result.match_rate_all.value


def test_the_confusion_matrix_accounts_for_every_record(scored):
    result, _run, labels = scored
    total = sum(sum(row.values()) for row in result.confusion.values())
    assert total == result.n_in
    for expected_state in result.confusion:
        expected_n = sum(1 for l in labels.values() if l.expected_state == expected_state)
        assert sum(result.confusion[expected_state].values()) == expected_n
    diagonal = sum(result.confusion[s][s] for s in result.confusion)
    assert diagonal == result.state_accuracy.numerator


# ---------------------------------------------------- the metrics can move


def test_a_missed_exception_shows_up_as_a_false_negative(sources, cfg, scored):
    """Negative control: silence a real NEVER_DEDUCTED and watch recall drop."""
    _result, run, labels = scored
    victim = next(v for v in run.verdicts if "NEVER_DEDUCTED" in v.exception_codes)
    broken = _replace_verdict(
        run, victim.refund_id, closure_state=CLOSED_MATCHED, exception_codes=[]
    )
    result = ev.evaluate(broken, sources, cfg, labels=labels)
    score = next(s for s in result.code_scores if s.code == "NEVER_DEDUCTED")
    assert score.fn == 1
    assert score.recall.value < 1.0
    assert result.exact_record_agreement.numerator == result.n_in - 1


def test_waving_a_seeded_failure_through_moves_the_money_number(sources, cfg, scored):
    """The false auto-match rate is the metric that must not be able to stay at zero."""
    _result, run, labels = scored
    victim = next(
        v
        for v in run.verdicts
        if labels[v.refund_id].expected_state == EXCEPTION
    )
    broken = _replace_verdict(
        run, victim.refund_id, closure_state=CLOSED_MATCHED, exception_codes=[]
    )
    result = ev.evaluate(broken, sources, cfg, labels=labels)
    assert result.false_auto_match_rate.numerator == 1
    assert result.false_auto_match_rate.value > 0


def test_closing_an_open_refund_only_moves_the_inclusive_rate(sources, cfg, scored):
    """This is why both denominators are printed.

    An OPEN refund called CLOSED_MATCHED tells a controller the money landed
    when it has not — but it is invisible to the manifest's "seeded failures"
    denominator, which counts only EXCEPTION and REJECTED_INPUT.
    """
    _result, run, labels = scored
    victim = next(v for v in run.verdicts if labels[v.refund_id].expected_state == OPEN)
    broken = _replace_verdict(
        run, victim.refund_id, closure_state=CLOSED_MATCHED, open_reasons=[]
    )
    result = ev.evaluate(broken, sources, cfg, labels=labels)
    assert result.false_auto_match_rate.numerator == 0
    assert result.false_auto_match_rate_incl_open.numerator == 1


def test_a_spurious_code_shows_up_as_a_false_positive(sources, cfg, scored):
    _result, run, labels = scored
    victim = next(v for v in run.verdicts if v.closure_state == CLOSED_MATCHED)
    broken = _replace_verdict(
        run, victim.refund_id, closure_state=EXCEPTION, exception_codes=["ARN_OVERDUE"]
    )
    result = ev.evaluate(broken, sources, cfg, labels=labels)
    score = next(s for s in result.code_scores if s.code == "ARN_OVERDUE")
    assert score.fp == 1
    assert score.precision.value < 1.0
    assert any(d.field == "closure_state" for d in result.disagreements)


def test_disagreements_are_capped_but_reported(sources, cfg, scored):
    _result, run, labels = scored
    broken = run
    for verdict in [v for v in run.verdicts if v.closure_state == CLOSED_MATCHED][:30]:
        broken = _replace_verdict(
            broken, verdict.refund_id, closure_state=EXCEPTION, exception_codes=["NEVER_DEDUCTED"]
        )
    result = ev.evaluate(broken, sources, cfg, labels=labels)
    assert 0 < len(result.disagreements) <= 20


# ------------------------------------------------------ duplicate sensitivity


def test_duplicate_recall_does_not_fall_as_the_window_widens(scored):
    """The one heuristic rule, reported at three windows (spec §6.6)."""
    result, _run, _labels = scored
    windows = sorted(result.duplicate_sensitivity)
    recalls = [result.duplicate_sensitivity[w].recall.value for w in windows]
    assert recalls == sorted(recalls)
    assert recalls[0] < recalls[-1], "the window must actually matter, or why report it"


def test_a_narrow_duplicate_window_keeps_precision(scored):
    """Tightening the window loses recall, never precision — it only removes pairs."""
    result, _run, _labels = scored
    for score in result.duplicate_sensitivity.values():
        assert score.fp == 0


# ------------------------------------------------------------- annotations


def test_informational_annotations_are_not_scored(scored):
    """NO_RMA_MATCH is emitted for goodwill refunds the generator never seeds."""
    _result, run, _labels = scored
    with_note = next(v for v in run.verdicts if "NO_RMA_MATCH" in v.annotations)
    assert "NO_RMA_MATCH" not in ev.observed_annotations(with_note)


def test_rejection_reasons_are_scored_as_annotations(scored):
    """Ground truth records them as annotations; the verdict keeps them typed."""
    _result, run, labels = scored
    rejected = next(v for v in run.verdicts if v.closure_state == REJECTED_INPUT)
    assert ev.observed_annotations(rejected) == labels[rejected.refund_id].expected_annotations


# ------------------------------------------------------------------ loading


def test_a_missing_ground_truth_file_says_what_to_do(tmp_path):
    with pytest.raises(FileNotFoundError, match="make data"):
        ev.load_ground_truth(tmp_path)


def test_ground_truth_can_be_loaded_from_a_directory(tmp_path):
    payload = {
        "rfnd_1": {
            "scenario": "happy_path",
            "expected_state": "CLOSED_MATCHED",
            "expected_codes": [],
            "expected_open_reasons": [],
            "expected_annotations": [],
            "expected_timing_flags": [],
            "payment_id": "pay_1",
            "note": "",
        }
    }
    (tmp_path / "ground_truth.json").write_text(json.dumps(payload))
    labels = ev.load_ground_truth(tmp_path)
    assert labels["rfnd_1"].expected_state == "CLOSED_MATCHED"
    assert labels["rfnd_1"].scenario == "happy_path"


def test_labels_and_verdicts_must_cover_the_same_refunds(sources, cfg, scored):
    """Zero silent drops, checked from the scoring side too."""
    _result, run, labels = scored
    trimmed = dict(list(labels.items())[:-1])
    with pytest.raises(AssertionError, match="same refunds"):
        ev.evaluate(run, sources, cfg, labels=trimmed)


def test_only_the_evaluator_reads_the_answer_key():
    """Positive control for the prohibition tested in test_loader.py."""
    import pathlib

    import rlc

    source = (pathlib.Path(rlc.__file__).parent / "evaluate.py").read_text(encoding="utf-8")
    assert "ground_truth.json" in source
