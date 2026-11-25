"""website-seo-opportunities' script scores Search Console rows deterministically."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema

SKILL = Path(__file__).resolve().parent.parent / "plugins" / "ai-search-suite" / "skills" / "website-seo-opportunities"
SITE = "https://www.example.com"


def _row(query: str, path: str, clicks: int, impressions: int, position: float) -> dict:
    return {"keys": [query, f"{SITE}{path}"], "clicks": clicks, "impressions": impressions, "ctr": clicks / impressions, "position": position}


def _gsc(rows: list) -> dict:
    return {"step": {"ok": True, "data": {"rows": rows, "responseAggregationType": "byPage"}}}


def _run(tmp_path: Path, inputs: dict) -> tuple[dict, int]:
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(inputs), encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "score.py"), "--input", str(path), "--out", str(tmp_path / "o.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    output = json.loads(done.stdout)
    schema = json.loads((SKILL / "assets" / "output.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(output, schema)
    return output, done.returncode


def _types(output: dict) -> list[tuple[str, str]]:
    return [(o["type"], o["query"]) for o in output["opportunities"]]


def test_a_low_ctr_title_is_a_quick_fix_with_its_extra_clicks(tmp_path: Path) -> None:
    output, code = _run(tmp_path, {"site_url": SITE, "current": _gsc([_row("invoice template", "/t", 26, 2600, 4.3)])})
    assert code == 0
    [item] = output["opportunities"]
    assert item["type"] == "ctr_gap"
    assert item["effort"] == "quick fix"
    # The default curve expects 6% at position 4: 2600 x 0.06 - 26 = 130.
    assert item["extra_clicks"] == 130
    assert "the benchmark CTR" in item["why"]
    assert output["target_urls"] == [f"{SITE}/t"]


def test_the_curve_is_the_sites_own_when_it_has_enough_queries(tmp_path: Path) -> None:
    rows = [_row(f"topic {n}", f"/p{n}", 30, 200, 4.0) for n in range(5)]  # 1,000 impressions at 15%
    rows.append(_row("weak title", "/weak", 10, 1000, 4.1))
    output, _ = _run(tmp_path, {"site_url": SITE, "current": _gsc(rows)})
    four = next(c for c in output["ctr_curve"] if c["position"] == "4")
    assert four["source"] == "site"
    weak = next(o for o in output["opportunities"] if o["query"] == "weak title")
    assert weak["type"] == "ctr_gap"
    assert "this site's CTR" in weak["why"]


def test_one_big_query_is_not_a_curve(tmp_path: Path) -> None:
    output, _ = _run(tmp_path, {"site_url": SITE, "current": _gsc([_row("only query", "/x", 10, 5000, 4.0)])})
    assert next(c for c in output["ctr_curve"] if c["position"] == "4")["source"] == "default"
    assert _types(output) == [("ctr_gap", "only query")]


def test_branded_queries_are_never_opportunities(tmp_path: Path) -> None:
    output, _ = _run(tmp_path, {"site_url": SITE, "current": _gsc([_row("example login", "/login", 1, 3000, 4.0)])})
    assert output["opportunities"] == []
    assert output["sentence"] == "No opportunity cleared the thresholds this period."


def test_split_pages_become_one_cannibalization_item(tmp_path: Path) -> None:
    rows = [_row("invoice software", "/pricing", 30, 1200, 9.1), _row("invoice software", "/features", 22, 1100, 10.4)]
    output, _ = _run(tmp_path, {"site_url": SITE, "current": _gsc(rows)})
    assert _types(output) == [("cannibalization", "invoice software")]
    item = output["opportunities"][0]
    assert item["page"] == f"{SITE}/pricing"
    assert item["competing_pages"] == [f"{SITE}/features"]
    assert item["target_position"] == 6


def test_decay_splits_ranking_loss_from_falling_demand(tmp_path: Path) -> None:
    current = [_row("recurring invoices", "/recurring", 12, 900, 13.6), _row("time tracking", "/time", 8, 400, 6.2)]
    previous = [_row("recurring invoices", "/recurring", 80, 1000, 4.2), _row("time tracking", "/time", 40, 1900, 6.0)]
    output, _ = _run(tmp_path, {"site_url": SITE, "current": _gsc(current), "previous": _gsc(previous)})
    decayed = [o for o in output["opportunities"] if o["type"] == "content_decay"]
    assert [d["page"] for d in decayed] == [f"{SITE}/recurring"]
    assert decayed[0]["extra_clicks"] == 68
    assert [d["page"] for d in output["demand_decay"]] == [f"{SITE}/time"]
    assert output["outcome"] == "ok"


def test_without_a_previous_period_decay_is_named_as_unchecked(tmp_path: Path) -> None:
    output, _ = _run(tmp_path, {"site_url": SITE, "current": _gsc([_row("a b c", "/a", 1, 100, 5.0)])})
    assert output["outcome"] == "partial"
    assert any("decay was not checked" in u for u in output["unknowns"])


def test_a_failed_current_query_is_an_error_not_a_guess(tmp_path: Path) -> None:
    output, code = _run(tmp_path, {"site_url": SITE, "current": {"step": {"ok": False, "error": "403"}}})
    assert code == 1
    assert output["outcome"] == "error"
    assert output["opportunities"] == []


def test_ranking_is_stable_and_caps_two_per_page(tmp_path: Path) -> None:
    rows = [_row(f"query {n}", "/same", 5, 1000, 4.0) for n in range(4)]
    first, _ = _run(tmp_path, {"site_url": SITE, "current": _gsc(rows)})
    second, _ = _run(tmp_path, {"site_url": SITE, "current": _gsc(list(reversed(rows)))})
    assert [o["query"] for o in first["opportunities"]] == ["query 0", "query 1"]
    assert first["opportunities"] == second["opportunities"]


def test_every_example_is_what_the_script_prints(tmp_path: Path) -> None:
    for case in sorted((SKILL / "assets" / "examples").glob("*.input.json")):
        expected = json.loads(case.with_name(case.name.replace(".input.", ".output.")).read_text(encoding="utf-8"))
        output, _ = _run(tmp_path, json.loads(case.read_text(encoding="utf-8")))
        assert output == expected, case.name
