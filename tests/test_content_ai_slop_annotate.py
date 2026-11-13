"""content-ai-slop-annotate's scripts: the pre-scan counts the mechanical tells and applies
the thresholds the same way every run (one instance of a pattern is borderline, a pattern is
flagged), and finalize checks the model's annotated report against the draft before saving.

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
FINALIZE = SKILL / "scripts" / "finalize.py"
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


def test_a_narrative_refrain_is_flagged_but_left_to_judgement() -> None:
    refrain = next(c for c in scan_fixture("T5-borderline.md")["categories"] if c["id"] == 7)
    assert refrain["decision"] == "flag"
    assert all(i["needs_judgment"] for i in refrain["instances"])


def test_a_when_when_when_run_is_binding() -> None:
    run = next(c for c in scan_fixture("T2-categories.md")["categories"] if c["id"] == 7)
    assert run["decision"] == "flag"
    assert not any(i["needs_judgment"] for i in run["instances"])


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


# --- finalize.py -------------------------------------------------------------------------

EM_DASH_TEXT = json.loads((EXAMPLES / "em-dash-pattern.input.json").read_text(encoding="utf-8"))["text"]
EM_DASH_REPORT = (FIXTURES / "em-dash-pattern.report.md").read_text(encoding="utf-8")
BORDERLINE_TEXT = json.loads((EXAMPLES / "borderline-only.input.json").read_text(encoding="utf-8"))["text"]
BORDERLINE_REPORT = json.loads(
    (EXAMPLES / "borderline-only.output.json").read_text(encoding="utf-8")
)["annotated_markdown"]


def finalize(tmp_path: Path, original: str, report: str, *args: str) -> tuple[int, dict, Path]:
    (tmp_path / "draft.md").write_text(original, encoding="utf-8")
    (tmp_path / "report.md").write_text(report, encoding="utf-8")
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(FINALIZE), "--original", str(tmp_path / "draft.md"),
         "--annotated", str(tmp_path / "report.md"), "--out-dir", str(out), *args],
        capture_output=True, text=True, check=False,
    )
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1, f"stdout must be exactly one JSON line, got {proc.stdout!r}"
    return proc.returncode, json.loads(lines[0]), out


def checks(result: dict) -> set[str]:
    return {p["check"] for p in result["problems"]}


def test_a_correct_report_is_saved_exactly_as_the_example_says(tmp_path: Path) -> None:
    code, result, out = finalize(tmp_path, EM_DASH_TEXT, EM_DASH_REPORT, "--name", "Slop-check")
    assert (code, result["problems"]) == (0, [])
    shown = json.loads((EXAMPLES / "em-dash-pattern.output.json").read_text(encoding="utf-8"))
    assert result["saved"]["bytes"] == shown["file"]["bytes"]
    assert result["saved"]["sha256"] == shown["file"]["sha256"]
    assert (out / "Slop-check.md").read_text(encoding="utf-8") == EM_DASH_REPORT


def test_borderline_only_report_passes_without_saving(tmp_path: Path) -> None:
    code, result, out = finalize(tmp_path, BORDERLINE_TEXT, BORDERLINE_REPORT, "--no-save")
    assert (code, result["problems"], result["saved"]) == (0, [], None)
    assert not out.exists()


def test_a_fix_containing_an_em_dash_is_rejected(tmp_path: Path) -> None:
    bad = EM_DASH_REPORT.replace("**on a plane or in a tunnel**", "**on a plane — or a tunnel**")
    code, result, out = finalize(tmp_path, EM_DASH_TEXT, bad)
    assert code == 1 and checks(result) == {"fix_has_tell"}
    assert result["saved"] is None and not out.exists()


def test_a_fix_containing_a_list_of_three_is_rejected(tmp_path: Path) -> None:
    bad = EM_DASH_REPORT.replace(
        "**reconnect, with no extra steps.**", "**reconnect, quickly, quietly, and safely.**"
    )
    assert "fix_has_tell" in checks(finalize(tmp_path, EM_DASH_TEXT, bad)[1])


def test_a_label_with_commentary_is_rejected(tmp_path: Path) -> None:
    bad = EM_DASH_REPORT.replace("*(#13 em dash)*", "*(#13 em dash, three in one sentence)*", 1)
    assert checks(finalize(tmp_path, EM_DASH_TEXT, bad)[1]) == {"label_format"}


def test_a_split_paragraph_is_a_changed_text(tmp_path: Path) -> None:
    bad = EM_DASH_REPORT.replace("Setup takes two minutes. Open", "Setup takes two minutes.\n\nOpen")
    assert checks(finalize(tmp_path, EM_DASH_TEXT, bad)[1]) == {"text_changed"}


def test_spacing_between_annotations_is_not_a_change(tmp_path: Path) -> None:
    spaced = EM_DASH_REPORT.replace("*(#13 em dash)* are saved", "*(#13 em dash)*   are saved")
    assert finalize(tmp_path, EM_DASH_TEXT, spaced, "--no-save")[1]["problems"] == []


def test_a_borderline_instance_annotated_inline_is_rejected(tmp_path: Path) -> None:
    bad = BORDERLINE_REPORT.replace(
        "Moreover, the export", "~~Moreover,~~ → **cut: the logic carries** *(#3 formulaic transition)* the export"
    )
    assert checks(finalize(tmp_path, BORDERLINE_TEXT, bad, "--no-save")[1]) == {"not_flagged"}


def test_a_flagged_category_left_unannotated_is_rejected(tmp_path: Path) -> None:
    header, _, _ = EM_DASH_REPORT.partition("---\n")
    unannotated = f"{header}---\n\n{EM_DASH_TEXT}\n"
    assert checks(finalize(tmp_path, EM_DASH_TEXT, unannotated)[1]) == {"missing_flag"}


def test_a_wrong_counts_line_is_rejected(tmp_path: Path) -> None:
    bad = EM_DASH_REPORT.replace("em dashes 3", "em dashes 2", 1)
    assert checks(finalize(tmp_path, EM_DASH_TEXT, bad)[1]) == {"counts_line"}


def test_accept_saves_a_report_that_still_has_problems(tmp_path: Path) -> None:
    bad = EM_DASH_REPORT.replace("**on a plane or in a tunnel**", "**on a plane — or a tunnel**")
    code, result, out = finalize(tmp_path, EM_DASH_TEXT, bad, "--accept")
    assert code == 1 and checks(result) == {"fix_has_tell"}
    assert (out / result["saved"]["file_name"]).exists()


def test_scratch_files_left_in_the_out_dir_are_removed(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "tmp-input.md").write_text(EM_DASH_TEXT, encoding="utf-8")
    (out / "Slop-check.md").write_text("an earlier file", encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text(EM_DASH_REPORT, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(FINALIZE), "--original", str(out / "tmp-input.md"),
         "--annotated", str(report), "--out-dir", str(out)],
        capture_output=True, text=True, check=True,
    )
    result = json.loads(proc.stdout)
    assert result["saved"]["file_name"] == "Slop-check-2.md"
    assert sorted(p.name for p in out.iterdir()) == ["Slop-check-2.md", "Slop-check.md"]


def test_the_skills_own_scratch_names_are_removed_even_when_not_passed(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "content-ai-slop-annotate.md").write_text(EM_DASH_TEXT, encoding="utf-8")
    code, result, _ = finalize(tmp_path, EM_DASH_TEXT, EM_DASH_REPORT)
    assert code == 0
    assert sorted(p.name for p in out.iterdir()) == ["Slop-check.md"]
