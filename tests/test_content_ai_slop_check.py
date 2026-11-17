"""The content-ai-slop-check examples are the reference reports: they must read the way
SKILL.md says a report reads, because a model copies their shape."""

import json
import re
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "content-ai-slop-check"
EXAMPLES = sorted((SKILL / "assets" / "examples").glob("*.output.json"))
RULEBOOK_WORDS = re.compile(r"threshold|guard|borderline|\(\d+ of \d+\)|/10\b|score", re.IGNORECASE)
TABLE_ROW = re.compile(r"^\|\s*\d+\s*\|", re.MULTILINE)
GUIDE = "How to read the marks:"


def _section(report: str, heading: str) -> str:
    match = re.search(rf"^### {re.escape(heading)}\n(.*?)(?=^### |\Z)", report, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def _load(path: Path) -> tuple[dict, str]:
    output = json.loads(path.read_text())
    draft = json.loads(path.with_name(path.name.replace(".output.", ".input.")).read_text())["text"]
    return output, draft


def test_there_are_examples():
    assert len(EXAMPLES) >= 2


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_report_reads_as_skill_md_says(path):
    output, _ = _load(path)
    report = output["report_markdown"]
    assert report.startswith("## AI slop check\n\n**Result:** ")
    assert not RULEBOOK_WORDS.search(report)
    assert "### Your draft, marked up" in report

    rows = TABLE_ROW.findall(_section(report, "What to change"))
    assert len(rows) == output["marks"]
    bullets = [line for line in _section(report, "Checked and left alone").splitlines() if line.startswith("- ")]
    assert len(bullets) == output["left_alone"]

    if output["marks"]:
        assert GUIDE in report and "Nothing is marked." not in report
    else:
        assert "### What to change" not in report
        assert "Nothing is marked." in report and GUIDE not in report
        assert report.count("~~") == 0
    if not output["left_alone"]:
        assert "### Checked and left alone" not in report


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_unmarked_draft_comes_back_verbatim(path):
    output, draft = _load(path)
    marked = output["report_markdown"].split("\n---\n\n", 1)[1]
    unmarked = re.sub(r"~~(.*?)~~ → \*\*.*?\*\* \*\(#\d+ [a-z -]+\)\*", r"\1", marked)
    unmarked = re.sub(r"^> \*\*SLOP #\d+, [a-z -]+:\*\*.*\n", "", unmarked, flags=re.MULTILINE)
    assert unmarked.strip() == draft.strip()


def test_prd_example_marks_each_single_empty_tell():
    output, _ = _load(SKILL / "assets" / "examples" / "prd-example.output.json")
    tags = re.findall(r"\*\(#(\d+) ([a-z -]+)\)\*", output["report_markdown"])
    assert tags == [
        ("1", "false contrast"),
        ("3", "filler transition"),
        ("4", "inflated word"),
        ("2", "rule of three"),
        ("11", "summary ending"),
    ]
