"""Tests for the explanation layer (SPEC.md §9, CLAUDE.md §9).

The model is allowed to write prose about an already-classified exception and
nothing else. These tests hold that line from three directions: the guards
reject bad output, every failure path lands on a template, and a run with a
model produces exactly the same classification as a run without one.

No test touches the network. The provider is injected.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from rlc import attributes, engine, explain
from rlc.entities import EXCEPTION


@pytest.fixture(scope="module")
def closed(sources, cfg):
    run = engine.close(sources, cfg)
    attributes.annotate(run, sources, cfg)
    return run


@pytest.fixture(scope="module")
def duplicate_facts(closed, sources, cfg):
    verdict = next(v for v in closed.verdicts if "DUPLICATE_SUSPECT" in v.exception_codes)
    return explain.build_facts(verdict, sources, cfg)


class FakeProvider:
    """Returns canned responses. Records what it was asked."""

    def __init__(self, responses=None, raises=None):
        self.responses = list(responses or [])
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.raises is not None:
            raise self.raises
        return self.responses.pop(0) if self.responses else self.responses_default()

    @staticmethod
    def responses_default() -> str:
        return json.dumps(
            {"explanation": "Nothing to report.", "recommended_action": "No action needed."}
        )


# --------------------------------------------------------------- template mode


def test_template_mode_needs_no_provider(closed, sources, cfg):
    """The demo must run with no API key at all (CLAUDE.md §9)."""
    report = explain.explain_all(closed.verdicts, sources, cfg, provider=None)
    exceptions = [v for v in closed.verdicts if v.closure_state == EXCEPTION]
    assert report.total == len(exceptions)
    assert report.counts == {explain.TEMPLATE_SOURCE: len(exceptions)}
    assert report.provider_errors == ()


def test_every_template_passes_its_own_guards(closed, sources, cfg):
    """Templates are the floor, so they must clear the bar they set for the model."""
    for verdict in closed.verdicts:
        if verdict.closure_state != EXCEPTION:
            continue
        facts = explain.build_facts(verdict, sources, cfg)
        template = explain.render_template(facts)
        for text in (template.text, template.recommended_action):
            assert explain.verify(text, facts) is None, (verdict.refund_id, text)
        # Hedging is checked across the pair, exactly as `explain_one` does it:
        # "Ask operations whether both were intended" is properly hedged even
        # though the hedging word sits in the sentence before it.
        combined = f"{template.text} {template.recommended_action}"
        assert explain.verify_claims(combined, facts) is None, (verdict.refund_id, combined)


def test_every_exception_code_has_its_own_template(closed, sources, cfg):
    """A generic sentence for a specific code is a wasted explanation."""
    seen: dict[str, str] = {}
    for verdict in closed.verdicts:
        if verdict.closure_state != EXCEPTION:
            continue
        facts = explain.build_facts(verdict, sources, cfg)
        code = facts.exception_codes[0]
        seen.setdefault(code, explain.render_template(facts).recommended_action)
    assert len(seen) >= 8
    assert len(set(seen.values())) == len(seen), "two codes share a recommended action"


def test_templates_are_deterministic(duplicate_facts):
    assert explain.render_template(duplicate_facts) == explain.render_template(duplicate_facts)


# ------------------------------------------------------------- the number guard


def test_a_number_from_the_facts_is_accepted(duplicate_facts):
    text = f"Refund {duplicate_facts.refund_id} for {duplicate_facts.amount_inr} is suspected."
    assert explain.verify(text, duplicate_facts) is None


def test_an_invented_number_is_rejected(duplicate_facts):
    text = "The customer should receive the money within 3 to 5 business days."
    reason = explain.verify(text, duplicate_facts)
    assert reason is not None
    assert "not present in the facts" in reason


def test_a_date_from_the_facts_is_accepted_in_either_form(cfg, closed, sources):
    verdict = next(v for v in closed.verdicts if "ARN_OVERDUE" in v.exception_codes)
    facts = explain.build_facts(verdict, sources, cfg)
    due = facts.evidence["arn_due"]
    year, month, day = due.split("-")
    assert explain.verify(f"No ARN has arrived by {due}.", facts) is None
    assert explain.verify(f"No ARN has arrived by {int(day)} of month {int(month)}, {year}.", facts) is None


def test_a_digit_inside_an_identifier_is_not_a_stray_number(duplicate_facts):
    """Ids are removed longest-first so their digits cannot read as figures."""
    text = f"Compare {duplicate_facts.refund_id} with the earlier refund."
    assert explain.verify(text, duplicate_facts) is None


# -------------------------------------------------------------- the claim guard


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("All four settlement legs have been verified.", "four legs"),
        ("This is a four-leg lifecycle.", "four legs"),
        ("The orphan refund was never deducted.", "orphan"),
        ("The money reached the customer last week.", "credited"),
        ("The amount was credited to the customer.", "credited"),
    ],
)
def test_forbidden_claims_are_rejected(duplicate_facts, text, fragment):
    """The number guard cannot catch these — "four" is a word, not a digit.

    Gemini produced the four-legs claim on the second output of the first live
    run, which is why this guard exists.
    """
    reason = explain.verify_claims(text, duplicate_facts)
    assert reason is not None and fragment in reason


def test_a_finding_needing_review_must_be_hedged(duplicate_facts):
    assert duplicate_facts.needs_human_review
    assert explain.verify_claims("This refund is a duplicate.", duplicate_facts) is not None
    assert explain.verify_claims("This appears to be a duplicate.", duplicate_facts) is None


def test_hedging_is_only_required_where_the_engine_asked_for_it(duplicate_facts):
    certain = dataclasses.replace(duplicate_facts, needs_human_review=False)
    assert explain.verify_claims("This refund is a duplicate.", certain) is None


# ------------------------------------------------------------- with a provider


def test_a_clean_model_response_is_used(duplicate_facts):
    good = json.dumps(
        {
            "explanation": f"Refund {duplicate_facts.refund_id} appears to repeat an earlier refund.",
            "recommended_action": "Ask operations to confirm both were intended.",
        }
    )
    result, error = explain.explain_one(duplicate_facts, FakeProvider([good]))
    assert error is None
    assert result.source == explain.MODEL_SOURCE
    assert "appears to repeat" in result.text


def test_an_invented_number_falls_back_to_the_template(duplicate_facts):
    bad = json.dumps(
        {
            "explanation": "The refund of 999.99 rupees is suspected to be a repeat.",
            "recommended_action": "Confirm with operations.",
        }
    )
    result, error = explain.explain_one(duplicate_facts, FakeProvider([bad]))
    assert error is None
    assert result.source == explain.REJECTED_SOURCE
    assert result.rejection_reason and "not present in the facts" in result.rejection_reason
    assert result.text == explain.render_template(duplicate_facts).text


def test_a_forbidden_claim_falls_back_to_the_template(duplicate_facts):
    bad = json.dumps(
        {
            "explanation": "All four legs have been verified for this suspected repeat.",
            "recommended_action": "Confirm with operations.",
        }
    )
    result, _error = explain.explain_one(duplicate_facts, FakeProvider([bad]))
    assert result.source == explain.REJECTED_SOURCE
    assert "four legs" in (result.rejection_reason or "")


def test_a_provider_failure_is_never_fatal(duplicate_facts):
    result, error = explain.explain_one(
        duplicate_facts, FakeProvider(raises=RuntimeError("Gemini HTTP 429: quota"))
    )
    assert result.source == explain.TEMPLATE_SOURCE
    assert error is not None and "429" in error


def test_malformed_json_falls_back(duplicate_facts):
    result, error = explain.explain_one(duplicate_facts, FakeProvider(["not json at all"]))
    assert result.source == explain.TEMPLATE_SOURCE
    assert error is not None


def test_a_missing_key_in_the_response_falls_back(duplicate_facts):
    result, error = explain.explain_one(
        duplicate_facts, FakeProvider([json.dumps({"explanation": "Something happened."})])
    )
    assert result.source == explain.TEMPLATE_SOURCE
    assert error is not None and "recommended_action" in error


def test_quota_exhaustion_still_explains_every_record(closed, sources, cfg):
    """The free-tier failure mode, which is the one that actually happened."""
    provider = FakeProvider(raises=RuntimeError("Gemini HTTP 429: quota"))
    report = explain.explain_all(closed.verdicts, sources, cfg, provider=provider)
    exceptions = [v for v in closed.verdicts if v.closure_state == EXCEPTION]
    assert report.total == len(exceptions)
    assert report.counts == {explain.TEMPLATE_SOURCE: len(exceptions)}
    assert len(report.provider_errors) == len(exceptions)


# ----------------------------------------------------------------- ordering


def test_explanations_are_ordered_by_exposure(closed, sources, cfg):
    report = explain.explain_all(closed.verdicts, sources, cfg)
    verdicts = closed.by_id()
    exposures = [verdicts[rid].exposure_paise for rid in report.explanations]
    assert exposures == sorted(exposures, reverse=True)


def test_capping_model_calls_spends_them_on_the_biggest_exposures(closed, sources, cfg):
    """A free-tier key runs out; it should run out on the cheap findings."""
    good = json.dumps(
        {"explanation": "A finding was recorded.", "recommended_action": "Review it."}
    )
    provider = FakeProvider([good] * 5)
    report = explain.explain_all(
        closed.verdicts, sources, cfg, provider=provider, max_model_calls=3
    )
    assert len(provider.calls) == 3
    assert report.counts[explain.MODEL_SOURCE] == 3
    ordered = list(report.explanations.values())
    assert all(e.source == explain.MODEL_SOURCE for e in ordered[:3])
    assert all(e.source == explain.TEMPLATE_SOURCE for e in ordered[3:])


# ----------------------------------------------------------------- boundaries


def test_the_model_never_changes_a_classification(closed, sources, cfg):
    """CLAUDE.md §9: not allowed to classify, match, or decide states."""
    before = [v.to_row() for v in closed.verdicts]
    for row in before:
        row.pop("explanation")
    good = json.dumps(
        {"explanation": "Some prose.", "recommended_action": "Some action."}
    )
    explain.explain_all(
        closed.verdicts, sources, cfg, provider=FakeProvider([good] * 100)
    )
    after = [v.to_row() for v in closed.verdicts]
    for row in after:
        row.pop("explanation")
    assert after == before


def test_the_facts_object_is_all_the_model_sees(duplicate_facts):
    """Whatever the prompt contains must be derivable from the facts object."""
    provider = FakeProvider([FakeProvider.responses_default()])
    explain.explain_one(duplicate_facts, provider)
    _system, user = provider.calls[0]
    assert json.loads(user) == duplicate_facts.to_prompt_dict()


def test_the_provider_is_off_by_default(cfg):
    """`llm.enabled: false` in config.yaml, so a clone runs keyless."""
    assert explain.build_provider(cfg) is None


def test_enabling_the_model_without_a_key_says_what_to_do(cfg, monkeypatch):
    enabled = dataclasses.replace(cfg, llm={**cfg.llm, "enabled": True})
    monkeypatch.delenv(enabled.llm.get("api_key_env", "GEMINI_API_KEY"), raising=False)
    with pytest.raises(RuntimeError, match="template mode"):
        explain.build_provider(enabled)


def test_an_unknown_provider_is_refused(cfg, monkeypatch):
    enabled = dataclasses.replace(
        cfg, llm={**cfg.llm, "enabled": True, "provider": "something-else"}
    )
    monkeypatch.setenv(enabled.llm.get("api_key_env", "GEMINI_API_KEY"), "x")
    with pytest.raises(ValueError, match="unsupported"):
        explain.build_provider(enabled)
