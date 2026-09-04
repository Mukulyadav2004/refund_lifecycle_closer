"""Tests for the loader (SPEC.md §2, §4, CLAUDE.md §12).

The loader decides nothing, so these tests are about two things only: that the
join graph is built correctly, and that the two ways of poisoning a run at load
time — reading the answer key, and pulling too narrow a window — are refused.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta

import pytest

from rlc import engine, loader
from rlc.calendar_utils import to_ist_date
from rlc.entities import Refund
from rlc.loader import (
    GroundTruthAccessError,
    PullWindowError,
    Sources,
    check_pull_window,
)


# ------------------------------------------------------------------ indexing


def test_every_refund_and_payment_is_indexed(sources, dataset):
    assert len(sources.refunds) == len(dataset.refunds)
    assert len(sources.payments_by_id) == len(dataset.payments)
    for refund in sources.refunds:
        assert sources.refunds_by_id[refund.id] is refund


def test_sibling_refunds_exclude_failed_ones(sources):
    """R(P) is non-failed by definition — failed refunds move no money."""
    for payment_id, siblings in sources.refunds_by_payment.items():
        expected = [r for r in siblings if r.status != "failed"]
        assert list(sources.sibling_refunds(payment_id)) == expected


def test_settlement_rows_are_keyed_on_the_refund_id(sources):
    for refund_id, rows in sources.recon_refunds_by_refund_id.items():
        assert all(row.is_refund and row.entity_id == refund_id for row in rows)


def test_ordering_does_not_depend_on_input_order(dataset, build_sources):
    """Two Sources built from the same records in different orders are identical."""
    forward = build_sources(dataset)
    backward = Sources.build(
        payments=list(reversed(dataset.payments)),
        refunds=list(reversed(dataset.refunds)),
        disputes=list(reversed(dataset.disputes)),
        settlements=list(reversed(dataset.settlements)),
        recon=list(reversed(dataset.recon)),
        returns_ledger=list(reversed(dataset.returns_ledger)),
    )
    assert [r.id for r in forward.refunds] == [r.id for r in backward.refunds]
    assert [r.entity_id for r in forward.recon] == [r.entity_id for r in backward.recon]


# --------------------------------------------------------- normalisation


@pytest.mark.parametrize(
    "acquirer_data, expected",
    [({}, None), ({"arn": None}, None), ({"arn": "10000000000000"}, "10000000000000")],
)
def test_acquirer_data_normalises_all_three_documented_shapes(acquirer_data, expected):
    """Razorpay's own samples show all three; the engine sees one field."""
    refund = Refund.from_api(
        {"id": "rfnd_x", "payment_id": "pay_x", "amount": 100, "acquirer_data": acquirer_data}
    )
    assert refund.arn == expected


def test_truncated_pagination_is_refused(tmp_path):
    """`count` disagreeing with `items` means a paginated pull was cut short."""
    path = tmp_path / "refunds.json"
    path.write_text(json.dumps({"entity": "collection", "count": 9, "items": []}))
    with pytest.raises(ValueError, match="declared count"):
        loader._read_collection(path)


# -------------------------------------------------------- the answer key


def test_the_loader_refuses_to_read_ground_truth(tmp_path):
    path = tmp_path / "ground_truth.json"
    path.write_text("{}")
    with pytest.raises(GroundTruthAccessError):
        loader._read_collection(path)


def _live_names_and_strings(source: str) -> set[str]:
    """Identifiers and string literals in a module, ignoring docstrings.

    Prose about the rule is fine; code that acts on the answer key is not, so
    this looks at what the module actually executes.
    """
    import ast

    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                found.add(node.value)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found.update(alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
    return found


def test_no_decision_module_reads_ground_truth():
    """Only rlc.evaluate may read the labels (CLAUDE.md §8).

    An engine that opens its own answer key is grading its own homework, and the
    failure is silent: every metric still prints, and every one of them is a lie.
    """
    import pathlib

    import rlc

    root = pathlib.Path(rlc.__file__).parent
    for name in ("engine.py", "invariants.py", "loader.py", "attributes.py"):
        path = root / name
        if not path.exists():
            continue
        offenders = {
            token
            for token in _live_names_and_strings(path.read_text(encoding="utf-8"))
            if "ground_truth" in token.lower() and token != "GROUND_TRUTH_FILENAME"
        }
        # loader.py names the file only in order to refuse it.
        allowed = (
            {"ground_truth.json", "_refuse_ground_truth", "GroundTruthAccessError"}
            if name == "loader.py"
            else set()
        )
        assert offenders <= allowed, f"{name} acts on the answer key: {offenders}"


def test_the_engine_does_not_import_the_evaluator():
    import pathlib

    import rlc

    source = (pathlib.Path(rlc.__file__).parent / "engine.py").read_text(encoding="utf-8")
    assert "evaluate" not in _live_names_and_strings(source)


# ----------------------------------------------------------- pull window


def test_the_real_window_is_wide_enough(sources, cfg):
    window = check_pull_window(sources, cfg, cfg.calendar())
    assert window.ok, window.problems
    assert window.recon_settled_last >= window.required_recon_end


def _narrow_to_period_end(dataset, cfg):
    """The classic mistake: pull recon for the reporting month and nothing after."""
    kept = [row for row in dataset.recon if to_ist_date(row.settled_at) <= cfg.run.period_end]
    return Sources.build(
        payments=dataset.payments,
        refunds=dataset.refunds,
        disputes=dataset.disputes,
        settlements=dataset.settlements,
        recon=kept,
        returns_ledger=dataset.returns_ledger,
    )


def test_a_single_month_recon_pull_is_refused(dataset, cfg):
    """CLAUDE.md §12: the recon endpoint is keyed on SETTLEMENT date.

    A refund created on 30 August is deducted in September, so a pull that stops
    at `period_end` cannot see it. The loader must say so rather than let the
    engine invent NEVER_DEDUCTED.
    """
    narrow = _narrow_to_period_end(dataset, cfg)
    window = check_pull_window(narrow, cfg, cfg.calendar())
    assert not window.ok
    assert any("recon coverage ends" in p for p in window.problems)
    with pytest.raises(PullWindowError):
        window.raise_if_bad()


def test_a_narrow_recon_pull_manufactures_never_deducted(dataset, cfg, build_sources):
    """The reason the check above exists, measured rather than asserted."""
    honest = engine.close(build_sources(dataset), cfg)
    narrow = engine.close(_narrow_to_period_end(dataset, cfg), cfg)
    seeded = honest.code_counts.get("NEVER_DEDUCTED", 0)
    manufactured = narrow.code_counts.get("NEVER_DEDUCTED", 0)
    assert manufactured > seeded * 2, (
        f"a single-month pull produced {manufactured} NEVER_DEDUCTED against {seeded} "
        "seeded — if this ever stops being true the trap has moved, not disappeared"
    )


def test_a_recon_window_that_stops_at_period_end_is_refused_by_config(sources, cfg):
    """Same trap, caught from the configured window instead of the data."""
    narrow_run = dataclasses.replace(cfg.run, recon_window_end=cfg.run.period_end)
    narrow_cfg = dataclasses.replace(cfg, run=narrow_run)
    window = check_pull_window(sources, narrow_cfg, cfg.calendar())
    assert not window.ok
    assert any("recon_window_end" in p for p in window.problems)


def test_missing_parents_are_reported_as_a_window_note(dataset, cfg):
    """A refund whose parent is outside the pull is a window symptom, not an exception."""
    dropped = dataset.refunds[0].payment_id
    trimmed = Sources.build(
        payments=[p for p in dataset.payments if p.id != dropped],
        refunds=dataset.refunds,
        disputes=dataset.disputes,
        settlements=dataset.settlements,
        recon=dataset.recon,
        returns_ledger=dataset.returns_ledger,
    )
    window = check_pull_window(trimmed, cfg, cfg.calendar())
    assert window.refunds_missing_parent >= 1
    assert any("NO_PARENT_PAYMENT" in note for note in window.notes)
