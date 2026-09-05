"""Tests for the dashboard's client-side code.

Only one thing here genuinely needs a JavaScript engine: the markdown renderer
that draws `report.md` in the browser. An earlier version of it span-locked on a
paragraph beginning with `**bold**` — the loop's stop condition treated a leading
`*` as a block marker, consumed nothing, and never advanced, which showed up as
"the report does not build". These tests run the real function against the real
report under a timeout, and skip where node is unavailable rather than pretend to
have checked.

Everything else is checked statically, because a static check that always runs
beats a dynamic one that usually skips.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from rlc.config import REPO_ROOT

APP_JS = REPO_ROOT / "web" / "app.js"
INDEX = REPO_ROOT / "web" / "index.html"
STYLES = REPO_ROOT / "web" / "styles.css"
RUNNER = REPO_ROOT / "tests" / "js" / "render_report.js"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@pytest.fixture(scope="module")
def report_md(tmp_path_factory, sources, cfg):
    from rlc import attributes, engine, evaluate as ev, explain, report as report_writer

    run = engine.close(sources, cfg)
    totals = attributes.annotate(run, sources, cfg)
    explanations = explain.explain_all(run.verdicts, sources, cfg)
    evaluation = ev.evaluate(run, sources, cfg, totals=totals)
    path = tmp_path_factory.mktemp("md") / "report.md"
    return report_writer.write_report_md(
        path, cfg, run, sources, totals, evaluation, explanations
    )


# --------------------------------------------------------------- static


def test_the_frontend_ships_no_external_resources():
    """No CDN: a container must serve everything from its own origin."""
    html = INDEX.read_text(encoding="utf-8")
    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert external == [], external


def test_the_markdown_loop_always_advances():
    """The structural fix: the paragraph branch consumes a line before testing.

    Without this, any stop condition that matches the first line of a paragraph
    is an infinite loop in the browser.
    """
    source = APP_JS.read_text(encoding="utf-8")
    assert "const buf = [lines[i++]];" in source


def test_the_stylesheet_defines_its_palette_at_the_root():
    css = STYLES.read_text(encoding="utf-8")
    assert ":root {" in css
    for token in ("--navy", "--blue", "--ink", "--green", "--red"):
        assert token in css, token


# ----------------------------------------------------------- with node


@needs_node
def test_the_report_renders_and_terminates(report_md):
    """The bug that read as "the report does not build"."""
    result = subprocess.run(
        ["node", str(RUNNER), str(APP_JS), str(report_md)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    stats = json.loads(result.stdout)
    assert stats["chars"] > 5_000
    assert stats["headings"] >= 10
    assert stats["tables"] >= 8
    assert stats["paragraphs"] >= 10
    assert stats["empty_paragraphs"] == 0


@needs_node
def test_a_paragraph_opening_with_bold_is_rendered(tmp_path):
    """The exact shape that hung: `**bold** ...` as the first line."""
    doc = tmp_path / "doc.md"
    doc.write_text(
        "# Title\n\n**Three legs verified, one leg evidenced.** Then more prose.\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(RUNNER), str(APP_JS), str(doc)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    stats = json.loads(result.stdout)
    assert stats["paragraphs"] == 1
    assert stats["tables"] == 1


@needs_node
def test_the_dashboard_script_parses():
    result = subprocess.run(
        ["node", "--check", str(APP_JS)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
