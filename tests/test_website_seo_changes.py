"""website-seo-changes' script says what changed since the last check's snapshot."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema

SKILL = Path(__file__).resolve().parent.parent / "plugins" / "ai-search-suite" / "skills" / "website-seo-changes"
EXAMPLES = SKILL / "assets" / "examples"
SCHEMA = json.loads((SKILL / "assets" / "output.schema.json").read_text(encoding="utf-8"))


def _run(tmp_path: Path, inputs: dict, steps: Path | None = None) -> tuple[dict, int]:
    source = tmp_path / "inputs.json"
    source.write_text(json.dumps(inputs), encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "changes.py"), "--input", str(source), "--out", str(tmp_path / "o.json"), "--steps-dir", str(steps or tmp_path / "steps")],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    output = json.loads(done.stdout)
    jsonschema.validate(output, SCHEMA)
    return output, done.returncode


def _example(case: str) -> dict:
    return json.loads((EXAMPLES / f"{case}.input.json").read_text(encoding="utf-8"))


def _texts(output: dict) -> dict[str, list[str]]:
    return {section["heading"]: [item["text"] for item in section["items"]] for section in output["report"]["sections"]}


def test_every_example_is_what_the_script_prints(tmp_path: Path) -> None:
    for case in sorted(EXAMPLES.glob("*.input.json")):
        expected = json.loads(case.with_name(case.name.replace(".input.", ".output.")).read_text(encoding="utf-8"))
        output, _ = _run(tmp_path, json.loads(case.read_text(encoding="utf-8")))
        assert output == expected, case.name


def test_the_first_run_is_the_baseline(tmp_path: Path) -> None:
    output, code = _run(tmp_path, _example("first-run"))
    assert code == 0 and output["outcome"] == "first_run"
    assert output["sheet_values"][0] == ["taken_at", "site", "score", "snapshot"]
    snapshot = json.loads(output["sheet_values"][1][3])
    assert snapshot == output["snapshot"]
    assert {fid: row["since"] for fid, row in snapshot["findings"].items()} == dict.fromkeys(snapshot["findings"], "2026-11-02")
    assert "Fix these first" in _texts(output)


def test_a_later_run_reports_only_what_changed(tmp_path: Path) -> None:
    output, code = _run(tmp_path, _example("changes-since-last-check"))
    assert code == 0 and output["outcome"] == "ok" and not output["quiet"]
    changes = output["changes"]
    assert [row["id"] for row in changes["new"]] == ["broken-links"]
    assert [row["id"] for row in changes["fixed"]] == ["noindex"]
    escalated = [row for row in changes["still_open"] if row["escalated"]]
    assert [(row["id"], row["age_days"], row["checks"]) for row in escalated] == [("keyword-untargeted:invoice software", 3, 2)]
    assert changes["ranks"] == [{"keyword": "invoice software", "before": None, "after": 8, "direction": "entered"}]
    assert changes["new_competitors"] == [{"keyword": "invoice software", "host": "wave.com"}]
    assert changes["speed"][0]["direction"] == "better"
    assert output["report"]["severity"] == "action_needed"
    assert output["report"]["title"].endswith("70/100 (▲ +8)")


def test_the_first_seen_date_is_carried_forward(tmp_path: Path) -> None:
    second, _ = _run(tmp_path, _example("changes-since-last-check"))
    third_inputs = copy.deepcopy(_example("changes-since-last-check"))
    third_inputs["previous"] = {"values": [second["sheet_values"][1]]}
    third_inputs["now"] = "2026-11-08T09:17:00Z"
    third, _ = _run(tmp_path, third_inputs)
    assert third["quiet"]
    assert third["report"]["summary"].startswith("No change since 2026-11-05.")
    row = next(r for r in third["changes"]["still_open"] if r["id"] == "keyword-untargeted:invoice software")
    assert (row["since"], row["age_days"], row["checks"]) == ("2026-11-02", 6, 3)
    assert "High, open 6 days (3rd check)" in _texts(third)["Act now: still open"][0]


def test_the_card_links_to_the_report(tmp_path: Path) -> None:
    output, _ = _run(tmp_path, _example("changes-since-last-check"))
    url = _example("changes-since-last-check")["report_url"]
    assert output["report"]["sources"] == [{"id": "S0", "title": "Full report and run details", "url": url}]
    assert f"[Open the report]({url})" in _texts(output)["Full report"][0]


def test_the_modules_are_read_from_the_earlier_steps(tmp_path: Path) -> None:
    inputs = _example("first-run")
    steps = tmp_path / "steps"
    steps.mkdir()
    for field, skill in (("audit", "website-seo-page-audit"), ("speed", "website-seo-performance"), ("gaps", "website-seo-content-gap")):
        (steps / f"{skill}.json").write_text(json.dumps(inputs.pop(field)), encoding="utf-8")
    output, _ = _run(tmp_path, inputs, steps)
    expected, _ = _run(tmp_path, _example("first-run"), tmp_path / "none")
    assert output == expected


def test_an_older_runs_files_are_not_used(tmp_path: Path) -> None:
    inputs = _example("first-run")
    steps = tmp_path / "steps"
    steps.mkdir()
    path = steps / "website-seo-page-audit.json"
    path.write_text(json.dumps(inputs.pop("audit")), encoding="utf-8")
    os.utime(path, (0, 0))
    output, code = _run(tmp_path, inputs, steps)
    assert code == 1 and output["outcome"] == "error"


def test_another_sites_snapshot_starts_a_new_baseline(tmp_path: Path) -> None:
    first, _ = _run(tmp_path, _example("first-run"))
    inputs = _example("first-run")
    inputs["site_url"] = "https://other.example.net"
    for module in ("audit", "speed", "gaps"):
        inputs[module]["site"] = "https://other.example.net"
    inputs["previous"] = first["sheet_values"][1][3]
    output, _ = _run(tmp_path, inputs)
    assert output["outcome"] == "first_run"
    assert "not other.example.net" in output["unknowns"][0]


def test_a_failed_audit_keeps_the_saved_snapshot(tmp_path: Path) -> None:
    first, _ = _run(tmp_path, _example("first-run"))
    inputs = _example("changes-since-last-check")
    inputs["previous"] = {"values": [first["sheet_values"][1]]}
    inputs["audit"] = {"step": {"ok": False, "error": "timeout"}}
    output, code = _run(tmp_path, inputs)
    assert code == 1 and output["outcome"] == "error"
    assert output["sheet_values"][1] == first["sheet_values"][1]


def test_a_missing_module_keeps_its_last_values(tmp_path: Path) -> None:
    first, _ = _run(tmp_path, _example("first-run"))
    inputs = _example("changes-since-last-check")
    inputs["previous"] = {"values": [first["sheet_values"][1]]}
    del inputs["speed"]
    output, _ = _run(tmp_path, inputs)
    assert output["changes"]["speed"] == []
    assert output["snapshot"]["speed"] == first["snapshot"]["speed"]
    assert any("Speed was not measured" in line for line in output["unknowns"])
