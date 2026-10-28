"""results-rank's script orders, recomputes and reports deterministically.

Run: uv run --with pytest --with pyyaml --with jsonschema pytest tests
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

SKILL = Path(__file__).resolve().parent.parent / "skills" / "results-rank"
SCRIPT = SKILL / "scripts" / "rank.py"
OUTPUT_SCHEMA = json.loads((SKILL / "assets" / "output.schema.json").read_text())
INPUT_SCHEMA = json.loads((SKILL / "assets" / "input.schema.json").read_text())
EXAMPLES = SKILL / "assets" / "examples"

ORDER = [{"field": "verdict", "order": ["Pursue", "Research", "Reject"]}, {"field": "score", "direction": "desc"}]


def run(tmp_path: Path, inputs: dict, *args: str, via: str = "env") -> tuple[int, dict]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("INPUT_")}
    if via == "env":
        env["INPUT_JSON"] = json.dumps(inputs)
    else:
        path = tmp_path / "input.json"
        path.write_text(json.dumps(inputs))
        env["INPUT_JSON_PATH"] = str(path)
    out = tmp_path / "results-rank.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out), *args],
        capture_output=True, text=True, env=env, check=False,
    )
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1, f"stdout must be one JSON line, got {proc.stdout!r} / {proc.stderr}"
    result = json.loads(lines[0])
    jsonschema.validate(result, OUTPUT_SCHEMA)
    assert json.loads(out.read_text()) == result
    return proc.returncode, result


def criterion(kind: str, weight: int, result: str, name: str = "c") -> dict:
    return {"criterion": name, "kind": kind, "weight": weight, "public_signal": True, "result": result}


def judge(name: str, verdict: str | None, score: int | None, criteria: list | None = None,
          status: str = "ok", tier: str | None = None, url: str | None = None) -> dict:
    domain = f"{name.lower().replace(' ', '')}.example"
    url = url or f"https://{domain}/"
    ir: dict = {
        "item": {"name": name}, "item_key": f"domain:{domain}", "item_label": f"{name} ({domain})",
        "status": status, "headline": f"{name} headline.", "source_ids": ["S1"],
    }
    out: dict = {"item_result": ir, "report": {
        "title": name, "summary": f"{name} summary.", "severity": "info", "sections": [],
        "sources": [{"id": "S1", "url": url, "title": name}], "unknowns": [],
    }}
    if verdict is not None:
        ir["verdict"] = out["verdict"] = verdict
    if score is not None:
        ir["score"] = out["fit_score"] = score
        ir["score_scale"] = "0-5"
    if criteria is not None:
        out["rubric_used"] = {"origin": "given", "criteria": criteria}
    if tier is not None:
        out["fit_tier"] = tier
    return out


def entry(output: dict | None, status: str = "ok", error: str | None = None, name: str = "X") -> dict:
    e: dict = {"item": {"index": 0, "item": {"name": name, "domain": f"{name.lower()}.example"},
                        "item_key": f"domain:{name.lower()}.example", "query": name},
               "outputs": {"search": {"results": []}, "judge": output}, "status": status}
    if error:
        e["error"] = error
    return e


def inputs(branches: list, **extra) -> dict:
    return {"top_n": 5, "item_kind": "company", "rank_by": ORDER, "now": "2026-10-28T09:00:00Z",
            "branches": {"branches": branches, "cut": [], "cut_count": 0}, **extra}


def labels(result: dict) -> list[str]:
    return [r["item_label"].split(" (")[0] for r in result["ranked"]]


@pytest.mark.parametrize("case", sorted(p.name[: -len(".input.json")] for p in EXAMPLES.glob("*.input.json")))
def test_examples_are_what_the_script_prints(tmp_path: Path, case: str) -> None:
    given = json.loads((EXAMPLES / f"{case}.input.json").read_text())
    jsonschema.validate(given, INPUT_SCHEMA)
    _, result = run(tmp_path, given)
    assert result == json.loads((EXAMPLES / f"{case}.output.json").read_text())


def test_reject_one_ranks_above_reject_zero(tmp_path: Path) -> None:
    branches = [entry(judge("Zero", "Reject", 0)), entry(judge("One", "Reject", 1)),
                entry(judge("Four", "Pursue", 4)), entry(judge("Two", "Research", 2))]
    _, result = run(tmp_path, inputs(branches))
    assert labels(result) == ["Four", "Two", "One", "Zero"]
    assert [r["rank"] for r in result["ranked"]] == [1, 2, 3, 4]


def test_ties_break_by_label_then_branch_order(tmp_path: Path) -> None:
    branches = [entry(judge("beta", "Pursue", 4)), entry(judge("Alpha", "Pursue", 4)),
                entry(judge("Gamma", "Pursue", 5))]
    _, result = run(tmp_path, inputs(branches, rank_by=[{"field": "verdict", "order": ["Pursue"]}]))
    assert labels(result) == ["Alpha", "beta", "Gamma"]


def test_missing_score_and_unlisted_verdict_come_last(tmp_path: Path) -> None:
    branches = [entry(judge("NoScore", "Pursue", None)), entry(judge("Odd", "Maybe", 5)),
                entry(judge("Low", "Pursue", 1))]
    _, result = run(tmp_path, inputs(branches))
    assert labels(result) == ["Low", "NoScore", "Odd"]


def test_must_have_not_met_caps_at_one(tmp_path: Path) -> None:
    crit = [criterion("must_have", 3, "not_met"), criterion("nice_to_have", 1, "met"),
            criterion("nice_to_have", 2, "met")]
    _, result = run(tmp_path, inputs([entry(judge("Capped", "Research", 2, crit, tier="Possible"))]))
    top = result["ranked"][0]
    assert (top["score"], top["verdict"]) == (1, "Reject")
    fields = {c["field"]: (c["judge_value"], c["computed_value"]) for c in result["corrections"]}
    assert fields == {
        "item_result.score": (2, 1), "fit_score": (2, 1),
        "item_result.verdict": ("Research", "Reject"), "verdict": ("Research", "Reject"),
        "fit_tier": ("Possible", "Weak"),
    }
    assert len(result["report"]["unknowns"]) == 1
    assert "Capped" in result["report"]["unknowns"][0]


def test_exclusion_met_scores_zero(tmp_path: Path) -> None:
    crit = [criterion("must_have", 3, "met"), criterion("nice_to_have", 2, "met"),
            criterion("exclusion", 1, "met")]
    _, result = run(tmp_path, inputs([entry(judge("Excluded", "Pursue", 5, crit))]))
    assert (result["ranked"][0]["score"], result["ranked"][0]["verdict"]) == (0, "Reject")


def test_weighted_formula_rounds_half_up(tmp_path: Path) -> None:
    # 5 × 3 ÷ 6 = 2.5 → 3; 5 × 4 ÷ 7 = 2.86 → 3 (the account-icp-qualification example).
    half = [criterion("must_have", 3, "met"), criterion("nice_to_have", 3, "unknown")]
    doc = [criterion("must_have", 3, "met"), criterion("nice_to_have", 1, "met"),
           criterion("nice_to_have", 1, "unknown"), criterion("nice_to_have", 2, "not_met")]
    _, result = run(tmp_path, inputs([entry(judge("Half", "Research", 3, half)),
                                      entry(judge("Doc", "Research", 3, doc))]))
    assert [r["score"] for r in result["ranked"]] == [3, 3]
    assert result["corrections"] == []
    assert result["report"]["unknowns"] == []


def test_low_score_from_unknowns_is_research_not_reject(tmp_path: Path) -> None:
    crit = [criterion("must_have", 3, "unknown"), criterion("nice_to_have", 2, "unknown"),
            criterion("nice_to_have", 1, "met")]
    _, result = run(tmp_path, inputs([entry(judge("Unknowns", "Reject", 1, crit))]))
    assert (result["ranked"][0]["score"], result["ranked"][0]["verdict"]) == (1, "Research")
    assert [c["field"] for c in result["corrections"]] == ["item_result.verdict", "verdict"]


def test_item_result_score_differing_from_fit_score_is_corrected(tmp_path: Path) -> None:
    crit = [criterion("must_have", 3, "met"), criterion("nice_to_have", 2, "met")]
    out = judge("Split", "Pursue", 5, crit)
    out["item_result"]["score"] = 4
    _, result = run(tmp_path, inputs([entry(out)]))
    assert result["ranked"][0]["score"] == 5
    assert result["corrections"] == [
        {"item_label": "Split (split.example)", "field": "item_result.score", "judge_value": 4, "computed_value": 5}
    ]


def test_non_rubric_judges_are_copied_unchanged(tmp_path: Path) -> None:
    out = judge("Plain", "Hot", 2)
    out["item_result"]["score_scale"] = "0-45"
    del out["fit_score"]
    _, result = run(tmp_path, inputs([entry(out)], rank_by=[{"field": "score"}]))
    top = result["ranked"][0]
    assert (top["verdict"], top["score"], top["score_scale"]) == ("Hot", 2, "0-45")
    assert result["corrections"] == []


def test_failed_and_skipped_branches(tmp_path: Path) -> None:
    branches = [
        entry(judge("Good", "Pursue", 4)),
        entry(None, status="failed", error="judge: the model call timed out", name="Kestrel"),
        entry(None, status="skipped"),
        entry(judge("Empty", None, None, status="skipped")),
    ]
    code, result = run(tmp_path, inputs(branches, cut_count=2))
    assert code == 0
    assert result["failed"] == [
        {"item_label": "Kestrel (kestrel.example)", "branch": "branches[1]", "reason": "judge: the model call timed out"}
    ]
    assert result["skipped_count"] == 2
    report = result["report"]
    assert report["severity"] == "attention"
    assert [s["heading"] for s in report["sections"]] == ["Top 1 of 1 company", "Not judged", "Not evaluated"]
    assert report["summary"].endswith("1 failed, 2 skipped. 2 not evaluated (over the limit).")


def test_judge_result_wins_over_a_failed_search(tmp_path: Path) -> None:
    out = judge("Sparse", "Research", None, status="no_evidence")
    _, result = run(tmp_path, inputs([entry(out, status="failed", error="search: provider error")]))
    assert labels(result) == ["Sparse"]
    assert result["failed"] == []


def test_shared_url_gets_one_id(tmp_path: Path) -> None:
    shared = "https://news.example.org/roundup"
    _, result = run(tmp_path, inputs([entry(judge("A", "Pursue", 5, url=shared)),
                                      entry(judge("B", "Pursue", 4, url=shared))]))
    assert [s["url"] for s in result["report"]["sources"]] == [shared]
    assert [r["source_ids"] for r in result["ranked"]] == [["S1"], ["S1"]]


def test_more_line_names_the_unshown_items(tmp_path: Path) -> None:
    branches = [entry(judge(n, "Pursue", s)) for n, s in (("A", 5), ("B", 4), ("C", 3))]
    _, result = run(tmp_path, inputs(branches, top_n=1))
    items = result["report"]["sections"][0]["items"]
    assert items[-1] == {"text": "…and 2 more: B (b.example), C (c.example)", "citations": [], "label": "more"}


def test_reads_inputs_from_input_json_path(tmp_path: Path) -> None:
    big = [entry(judge(f"Co{i:03d}", "Pursue", i % 6)) for i in range(100)]
    for e in big:  # push the inputs past 64 KiB, as the platform would
        e["outputs"]["search"] = {"results": [{"url": "https://x.example/", "content": "x" * 800}]}
    given = inputs(big)
    assert len(json.dumps(given)) > 64 * 1024
    code, result = run(tmp_path, given, via="path")
    assert code == 0
    assert len(result["ranked"]) == 100
    scores = [r["score"] for r in result["ranked"]]
    assert scores == sorted(scores, reverse=True)


def test_v1_step_outputs_file(tmp_path: Path) -> None:
    steps = {
        "prepare": {"slots": [
            {"i": 0, "item_key": "domain:a.example", "label": "A (a.example)", "query": "A"},
            {"i": 1, "item_key": "domain:k.example", "label": "Kestrel (k.example)", "query": "K"},
            None,
        ]},
        "judge-1": judge("A", "Pursue", 4),
        "judge-2": {"status": "error", "error": "timed out"},
        "judge-3": {},
    }
    path = tmp_path / "steps.json"
    path.write_text(json.dumps(steps))
    given = {"top_n": 3, "item_kind": "company", "rank_by": ORDER}
    _, result = run(tmp_path, given, "--steps", str(path), "--now", "2026-10-28T09:00:00Z")
    assert labels(result) == ["A"]
    assert result["failed"] == [{"item_label": "Kestrel (k.example)", "branch": "judge-2", "reason": "timed out"}]
    assert result["skipped_count"] == 1
    assert result["report"]["meta"]["generated_at"] == "2026-10-28T09:00:00Z"


def test_bad_input_prints_a_valid_error_report(tmp_path: Path) -> None:
    code, result = run(tmp_path, {"top_n": 3, "item_kind": "company", "rank_by": ORDER})
    assert code == 1
    assert result["ranked"] == []
    assert result["report"]["severity"] == "attention"
    assert "no evidence" in result["report"]["summary"]


def test_contact_details_never_reach_the_output(tmp_path: Path) -> None:
    out = judge("Jo", "Hot", 3)
    out["item_result"]["item_key"] = "email:jo@jo.example"
    failed = entry(None, status="failed", error="lookup failed for jo@jo.example", name="Kim")
    _, result = run(tmp_path, inputs([entry(out), failed], rank_by=[{"field": "score"}]))
    text = json.dumps({k: v for k, v in result.items()})
    assert "@" not in text
    assert "item_key" not in result["ranked"][0]


def test_a_failed_step_inside_a_judged_branch_is_named(tmp_path: Path) -> None:
    """Key-people pilot 31ecec67: one of nine searches failed on a gateway error and the
    report read "0 failed". The platform now passes the branch's step_errors."""
    hurt = entry(judge("Voltage Park", "Research", 2), name="VoltagePark")
    hurt["step_errors"] = {"search-role-1": "MCPCredentialError: Could not mint a gateway token"}
    _, result = run(tmp_path, inputs([entry(judge("Acme", "Pursue", 4), name="Acme"), hurt]))

    assert result["failed"] == [], "the branch itself was judged"
    assert "1 step failed inside 1 company." in result["report"]["summary"]
    [note] = [u for u in result["report"]["unknowns"] if "search-role-1" in u]
    assert note.startswith("Voltage Park (voltagepark.example): step search-role-1 failed (MCPCredentialError")
    assert note.endswith("judged without it.")


def test_no_step_errors_no_note(tmp_path: Path) -> None:
    _, result = run(tmp_path, inputs([entry(judge("Acme", "Pursue", 4), name="Acme")]))
    assert "failed inside" not in result["report"]["summary"]
    assert not [u for u in result["report"]["unknowns"] if "step " in u]
