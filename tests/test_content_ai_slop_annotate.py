"""content-ai-slop-annotate's pre-scan counts the mechanical tells and applies the thresholds
the same way every run: one instance of a pattern is borderline, a pattern is flagged.

Run: uv run --with pytest --with pyyaml --with jsonschema pytest tests
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "content-ai-slop-annotate"
SCRIPT = SKILL / "scripts" / "prescan.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "content-ai-slop-annotate"
EXAMPLES = SKILL / "assets" / "examples"


def scan(text: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={**os.environ, "INPUT_TEXT": text},
        capture_output=True,
        text=True,
        check=True,
    )
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1, f"stdout must be exactly one JSON line, got {proc.stdout!r}"
    return json.loads(lines[0])


def scan_fixture(name: str) -> dict:
    return scan((FIXTURES / name).read_text(encoding="utf-8"))


def decisions(result: dict) -> dict[int, str]:
    return {c["id"]: c["decision"] for c in result["categories"] if c["decision"] != "none"}


def test_listing_copy_flags_its_four_triplets() -> None:
    result = scan_fixture("T1-v1-listing.md")
    assert result["counts"]["triplets"] == 4
    assert decisions(result) == {2: "flag"}


def test_one_paragraph_per_category_hits_every_counted_category() -> None:
    result = scan_fixture("T2-categories.md")
    assert decisions(result) == {n: "flag" for n in (1, 2, 3, 4, 5, 7, 8, 9, 12, 13)}
    assert result["counts"] == {
        "triplets": 3,
        "transitions": 4,
        "false_contrasts": 2,
        "em_dashes": 4,
    }
    cadence = next(c for c in result["categories"] if c["id"] == 8)
    assert cadence["instances"][0]["sentence_word_counts"] == [14, 15, 15, 15, 16, 15]


def test_short_post_with_scaffolding_flags_only_category_10() -> None:
    assert decisions(scan_fixture("T3-short-scaffolding.md")) == {10: "flag"}


def test_clean_text_has_no_findings() -> None:
    result = scan_fixture("T4-clean.md")
    assert decisions(result) == {}
    assert result["counts_line"] == "triplets 0 · transitions 0 · false contrasts 0 · em dashes 0"


def test_single_instances_stay_borderline() -> None:
    got = decisions(scan_fixture("T5-borderline.md"))
    assert {k: got[k] for k in (1, 2, 3, 13)} == dict.fromkeys((1, 2, 3, 13), "borderline")


def test_a_refrain_is_flagged_but_left_to_judgement() -> None:
    refrain = next(c for c in scan_fixture("T5-borderline.md")["categories"] if c["id"] == 7)
    assert refrain["decision"] == "flag"
    assert all(i["needs_judgment"] for i in refrain["instances"])


def test_model_judged_categories_are_named() -> None:
    assert [c["id"] for c in scan_fixture("T4-clean.md")["model_judged"]] == [6, 11]


def test_empty_input_exits_2_with_nothing_on_stdout() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={**os.environ, "INPUT_TEXT": "   "},
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""


def test_reads_the_draft_from_a_file(tmp_path: Path) -> None:
    draft = tmp_path / "draft.md"
    draft.write_text((FIXTURES / "T1-v1-listing.md").read_text(encoding="utf-8"), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--file", str(draft)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(proc.stdout)["counts"]["triplets"] == 4


@pytest.mark.parametrize("case", ["borderline-only", "em-dash-pattern"])
def test_examples_agree_with_the_script(case: str) -> None:
    """The documented outputs must be what the pre-scan actually decides for their input."""
    given = json.loads((EXAMPLES / f"{case}.input.json").read_text(encoding="utf-8"))
    shown = json.loads((EXAMPLES / f"{case}.output.json").read_text(encoding="utf-8"))
    result = scan(given["text"])

    assert shown["counts"] == result["counts"]
    assert shown["counts_line"] == result["counts_line"]
    flagged = {c["id"]: c["count"] for c in result["categories"] if c["decision"] == "flag"}
    assert {f["category"]: f["count"] for f in shown["flagged"]} == flagged
    borderline = sorted(
        (c["id"], i["text"])
        for c in result["categories"]
        if c["decision"] == "borderline"
        for i in c["instances"]
    )
    assert sorted((b["category"], b["text"]) for b in shown["borderline"]) == borderline
    assert f"**Counts:** {result['counts_line']}" in shown["annotated_markdown"]
