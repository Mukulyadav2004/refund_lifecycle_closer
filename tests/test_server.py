"""Tests for the dashboard server (stdlib `http.server`).

Payload builders are pure functions of an `AppState`, so most of this needs no
socket. One integration test binds a loopback server on an ephemeral port to
cover routing and the static-file guard — loopback only, never the network.
"""

from __future__ import annotations

import json
import shutil
import threading
import urllib.request
from urllib.error import HTTPError

import pytest

from rlc import server
from rlc.entities import CLOSED_MATCHED, EXCEPTION


@pytest.fixture(scope="module")
def state(cfg):
    return server.build_state(cfg)


@pytest.fixture(scope="module")
def live(cfg, state):
    httpd = server.create_server(cfg, state, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return response.status, response.read()


# ------------------------------------------------------------------- state


def test_the_state_is_the_same_pipeline_the_cli_runs(state):
    """No second implementation: the dashboard closes the month the same way."""
    assert len(state.run.verdicts) == len(state.sources.refunds)
    assert sum(state.run.state_counts.values()) == len(state.run.verdicts)
    assert state.evaluation is not None
    assert state.totals.control.unexplained_paise == 0


def test_data_is_generated_when_a_fresh_container_has_none(cfg, tmp_path):
    """A deploy ships no data — it is gitignored — so startup makes it."""
    target = tmp_path / "synthetic"
    assert not target.exists()
    server.ensure_data(cfg, target)
    assert (target / "refunds.json").exists()
    assert (target / "ground_truth.json").exists()


# ---------------------------------------------------------------- payloads


def test_summary_reports_the_identity_and_the_control_total(state):
    payload = server.summary_payload(state)
    assert payload["identity"]["holds"]
    assert payload["identity"]["n_in"] == payload["identity"]["sum"]
    assert payload["control"]["unexplained_paise"] == 0
    assert payload["leakage"]["bps"] == state.totals.leakage.leakage_bps


def test_summary_carries_denominators_for_every_rate(state):
    """CLAUDE.md §8 travels to the UI: no rate without its numerator and denominator."""
    payload = server.summary_payload(state)
    for name, ratio in payload["rates"].items():
        assert set(ratio) == {"numerator", "denominator", "pct"}, name
        assert ratio["denominator"] > 0, name


def test_legs_are_three_verified_and_one_evidenced(state):
    legs = server.summary_payload(state)["legs"]
    assert [leg["status"] for leg in legs] == [
        "verified", "verified", "verified", "evidenced only"
    ]


def test_settlement_and_evidence_failures_are_separate_counts(state):
    failures = server.summary_payload(state)["leg_failures"]
    assert set(failures) == {"settlement", "evidence"}
    assert failures["evidence"] == state.run.code_counts.get("ARN_OVERDUE", 0)


def test_records_filter_by_state_and_code(state):
    exceptions = server.records_payload(state, closure_state=EXCEPTION, limit=500)
    assert exceptions["total"] == state.run.state_counts[EXCEPTION]
    assert all(r["closure_state"] == EXCEPTION for r in exceptions["items"])

    duplicates = server.records_payload(state, code="DUPLICATE_SUSPECT", limit=500)
    assert duplicates["total"] == state.run.code_counts["DUPLICATE_SUSPECT"]
    assert all(r["needs_human_review"] for r in duplicates["items"])


def test_records_are_sorted_by_exposure(state):
    items = server.records_payload(state, limit=500)["items"]
    exposures = [r["exposure_paise"] for r in items]
    assert exposures == sorted(exposures, reverse=True)


def test_records_paginate(state):
    first = server.records_payload(state, limit=10, offset=0)
    second = server.records_payload(state, limit=10, offset=10)
    assert first["total"] == second["total"] == len(state.run.verdicts)
    assert len(first["items"]) == len(second["items"]) == 10
    assert {r["refund_id"] for r in first["items"]} & {
        r["refund_id"] for r in second["items"]
    } == set()


def test_a_record_carries_its_explanation_and_evidence(state):
    verdict = next(v for v in state.run.verdicts if v.closure_state == EXCEPTION)
    record = server._record(state, verdict)
    assert record["explanation"]
    assert record["recommended_action"]
    assert record["explanation_source"]
    assert set(record["legs"]) == {"1", "2", "3", "4"}


def test_accuracy_scores_every_code_and_all_three_windows(state):
    payload = server.accuracy_payload(state)
    assert payload["available"]
    assert len(payload["codes"]) == 9
    assert len(payload["duplicate_sensitivity"]) == 3


def test_accuracy_degrades_cleanly_without_ground_truth(cfg, tmp_path):
    """Real merchant data has no labels; the dashboard must still work."""
    from rlc.loader import default_data_dir

    target = tmp_path / "unlabelled"
    shutil.copytree(default_data_dir(cfg), target)
    (target / "ground_truth.json").unlink()
    unlabelled = server.build_state(cfg, target)
    assert unlabelled.evaluation is None
    assert server.accuracy_payload(unlabelled)["available"] is False
    assert server.summary_payload(unlabelled)["identity"]["holds"]
    assert "rates" not in server.summary_payload(unlabelled)


def test_assumptions_are_served_as_assumptions(state, cfg):
    payload = server.assumptions_payload(state)
    assert len(payload["assumptions"]) == len(cfg.assumptions_table())
    assert len(payload["codes"]) == 9
    heuristics = [c for c in payload["codes"] if "HEURISTIC" in c["nature"]]
    assert [c["code"] for c in heuristics] == ["DUPLICATE_SUSPECT"]


# ------------------------------------------------------- the live control


def test_narrowing_the_duplicate_window_changes_the_result(cfg):
    """The demo's centrepiece: the threshold is doing real work, live."""
    wide = server.build_state(cfg, duplicate_window_seconds=86_400)
    narrow = server.build_state(cfg, duplicate_window_seconds=1_800)
    assert narrow.run.code_counts.get("DUPLICATE_SUSPECT", 0) < wide.run.code_counts[
        "DUPLICATE_SUSPECT"
    ]
    # Refunds that stop being flagged do not vanish — they close instead.
    assert narrow.run.state_counts[CLOSED_MATCHED] > wide.run.state_counts[CLOSED_MATCHED]
    assert sum(narrow.run.state_counts.values()) == sum(wide.run.state_counts.values())


# ------------------------------------------------------------- integration


def test_health_check_answers(live):
    status, body = get(live, "/healthz")
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_the_dashboard_and_its_assets_are_served(live):
    for path, marker in (
        ("/", b"Refund Lifecycle"),
        ("/static/styles.css", b"--navy"),
        ("/static/app.js", b"renderOverview"),
    ):
        status, body = get(live, path)
        assert status == 200, path
        assert marker in body, path


def test_the_api_answers_over_http(live):
    status, body = get(live, "/api/summary")
    assert status == 200
    assert json.loads(body)["identity"]["holds"]


def test_unknown_routes_are_not_found(live):
    with pytest.raises(HTTPError) as excinfo:
        get(live, "/api/definitely-not-a-route")
    assert excinfo.value.code == 404


def test_static_serving_refuses_path_traversal(live):
    """`/static/` must not become a file browser for the repo."""
    with pytest.raises(HTTPError) as excinfo:
        get(live, "/static/../config.yaml")
    assert excinfo.value.code == 404
