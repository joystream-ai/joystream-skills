"""website-seo-report's script renders the modules' outputs as an HTML page and a PDF."""

from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

SKILL = Path(__file__).resolve().parent.parent / "plugins" / "ai-search-suite" / "skills" / "website-seo-report"
EXAMPLES = SKILL / "assets" / "examples"
FAKE_CHROME = """#!/usr/bin/env python3
import sys
target = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--print-to-pdf="))
{body}
"""


def _chrome(tmp_path: Path, body: str = 'open(target, "wb").write(b"%PDF-1.4 fake")') -> str:
    path = tmp_path / "chrome"
    path.write_text(FAKE_CHROME.format(body=body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _run(tmp_path: Path, inputs: dict, chrome: str | None = "fake") -> tuple[dict, int, Path]:
    source = tmp_path / "inputs.json"
    source.write_text(json.dumps(inputs), encoding="utf-8")
    out_dir = tmp_path / "out"
    env = dict(os.environ, CHROME_PATH=_chrome(tmp_path) if chrome == "fake" else (chrome or str(tmp_path / "no-chrome")))
    done = subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "render.py"), "--input", str(source), "--out", str(tmp_path / "o.json"), "--out-dir", str(out_dir), "--steps-dir", str(tmp_path / "steps")],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=60,
    )
    output = json.loads(done.stdout)
    jsonschema.validate(output, json.loads((SKILL / "assets" / "output.schema.json").read_text(encoding="utf-8")))
    return output, done.returncode, out_dir


def _example(case: str) -> dict:
    return json.loads((EXAMPLES / f"{case}.input.json").read_text(encoding="utf-8"))


@pytest.fixture
def full(tmp_path: Path) -> tuple[dict, str]:
    output, code, out_dir = _run(tmp_path, _example("full-report"))
    assert code == 0
    return output, (out_dir / "seo-report.html").read_text(encoding="utf-8")


def test_a_full_run_writes_the_pdf_and_the_html(full: tuple[dict, str], tmp_path: Path) -> None:
    output, _ = full
    assert output["outcome"] == "ok"
    assert [f["kind"] for f in output["files"]] == ["pdf", "html"]
    assert (tmp_path / "out" / "seo-report.pdf").read_bytes().startswith(b"%PDF-")
    assert output["sections"] == ["summary", "changes", "scorecard", "speed", "fixes", "findings", "unknowns"]


def test_the_page_shows_the_modules_numbers(full: tuple[dict, str]) -> None:
    _, page = full
    inputs = _example("full-report")
    brief = inputs["brief"]["step"]["data"]
    assert f'{brief["score"]}<span>/100</span>' in page
    for action in brief["actions"]:
        assert action["change"] in page
    for category in inputs["audit"]["categories"]:
        assert f'{category["score"]}/10' in page
    assert "4.6 s" in page and "Poor" in page  # the homepage's LCP and its rating
    assert "Real users (Chrome UX Report" in page


def test_a_failed_or_missing_module_is_named_not_drawn(full: tuple[dict, str]) -> None:
    output, page = full
    assert "content_gaps" not in output["sections"]
    assert "Content gaps: not checked this run" in page
    assert "Search Console: no click data this run" in page


def test_input_text_is_escaped_and_the_page_loads_nothing(tmp_path: Path) -> None:
    inputs = _example("full-report")
    brief = copy.deepcopy(inputs["brief"]["step"]["data"])
    brief["actions"][0]["change"] = "<script>alert(1)</script> Rewrite"
    brief["report"]["summary"] = '"><img src=x onerror=alert(1)>'
    inputs["brief"] = brief
    _, _, out_dir = _run(tmp_path, inputs)
    page = (out_dir / "seo-report.html").read_text(encoding="utf-8")
    assert "<script" not in page and "<img" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Rewrite" in page
    assert "default-src 'none'" in page


def test_without_chromium_the_html_is_still_written(tmp_path: Path) -> None:
    output, code, out_dir = _run(tmp_path, _example("full-report"), chrome=None)
    assert code == 0
    assert output["outcome"] == "partial"
    assert [f["kind"] for f in output["files"]] == ["html"]
    assert "Chromium was not found" in output["unknowns"][0]
    assert not (out_dir / "seo-report.pdf").exists()


def test_a_chromium_that_prints_nothing_is_not_a_pdf(tmp_path: Path) -> None:
    broken = _chrome(tmp_path, 'open(target, "wb").write(b"<html>error</html>")')
    output, _, out_dir = _run(tmp_path, _example("full-report"), chrome=broken)
    assert output["outcome"] == "partial"
    assert "did not produce a PDF" in output["unknowns"][0]
    assert not (out_dir / "seo-report.pdf").exists()


def test_only_the_audit_still_gives_a_report(tmp_path: Path) -> None:
    inputs = _example("full-report")
    output, code, _ = _run(tmp_path, {"audit": inputs["audit"], "now": inputs["now"]})
    assert code == 0
    assert output["score"] == inputs["audit"]["overall_score"]
    assert "changes" not in output["sections"] and "fixes" not in output["sections"]
    assert {"summary", "scorecard", "findings"} <= set(output["sections"])


def test_search_console_adds_the_clicks_column_and_chart(tmp_path: Path) -> None:
    inputs = _example("full-report")
    brief = inputs["brief"]["step"]["data"]
    brief["actions"][0]["extra_clicks"] = 130
    inputs["opportunities"] = {"opportunities": [{"query": "invoice template", "extra_clicks": 130, "position": 4.3}], "extra_clicks_total": 130}
    output, _, out_dir = _run(tmp_path, inputs)
    page = (out_dir / "seo-report.html").read_text(encoding="utf-8")
    assert "opportunities" in output["sections"]
    assert "<th>Clicks</th>" in page and "+130" in page
    assert "+130</b> clicks / 28 days available" in page


def test_a_previous_audit_shows_the_change(tmp_path: Path) -> None:
    inputs = _example("full-report")
    inputs["audit"]["changes"] = {"previous_score": 44, "score_delta": 6, "new": ["broken-links"], "fixed": ["lang-missing", "noindex"], "still_open": 3}
    _, _, out_dir = _run(tmp_path, inputs)
    assert "▲ +6 since the last audit · 2 fixed · 1 new" in (out_dir / "seo-report.html").read_text(encoding="utf-8")


def test_every_example_is_what_the_script_prints(tmp_path: Path) -> None:
    for case in sorted(EXAMPLES.glob("*.input.json")):
        expected = json.loads(case.with_name(case.name.replace(".input.", ".output.")).read_text(encoding="utf-8"))
        output, _, _ = _run(tmp_path, json.loads(case.read_text(encoding="utf-8")))
        for item in (*output["files"], *expected["files"]):
            item["path"], item["bytes"] = Path(item["path"]).name, 0
        assert output == expected, case.name


def _save_steps(tmp_path: Path, inputs: dict, **overrides: dict) -> None:
    steps = tmp_path / "steps"
    steps.mkdir()
    for name, skill in (("audit", "website-seo-page-audit"), ("speed", "website-seo-performance")):
        (steps / f"{skill}.json").write_text(json.dumps({**inputs[name], **overrides}), encoding="utf-8")


def test_the_modules_are_read_from_the_earlier_steps(tmp_path: Path, full: tuple[dict, str]) -> None:
    inputs = _example("full-report")
    _save_steps(tmp_path, inputs)
    output, code, _ = _run(tmp_path, {"brief": inputs["brief"], "now": inputs["now"]})
    assert code == 0
    assert output["sections"] == full[0]["sections"]


def test_another_sites_or_an_older_runs_files_are_not_used(tmp_path: Path) -> None:
    inputs = _example("full-report")
    _save_steps(tmp_path, inputs, site="https://other.example.net")
    output, _, _ = _run(tmp_path, {"brief": inputs["brief"], "now": inputs["now"]})
    assert "scorecard" not in output["sections"] and "speed" not in output["sections"]
    later = tmp_path / "later"
    later.mkdir()
    _save_steps(later, inputs)
    output, _, _ = _run(later, {"brief": inputs["brief"], "now": "2099-01-01T00:00:00Z"})
    assert "scorecard" not in output["sections"] and "speed" not in output["sections"]
