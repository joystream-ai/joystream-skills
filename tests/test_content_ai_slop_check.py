"""The content-ai-slop-check examples are the reference reports: they must read the way
SKILL.md says a report reads, because a model copies their shape."""

import json
import re
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "content-ai-slop-check"
EXAMPLES = sorted((SKILL / "assets" / "examples").glob("*.output.json"))
VERDICTS = {"ready_to_post": "Ready to post", "needs_edits": "Needs edits", "needs_rewrite": "Needs a rewrite"}
RULEBOOK_WORDS = re.compile(r"threshold|guard|borderline|\(\d+ of \d+\)|/10\b|score|#\d", re.IGNORECASE)
OLD_NOTE_TAG = re.compile(r"\bSLOP\b")
MARK = re.compile(r"~~(.*?)~~ → \*\*(.*?)\*\* \*\((.*?)\)\*")
NOTE = re.compile(r"^> \*\*Paragraph note, [a-z -]+:\*\*.*\n", re.MULTILINE)
GUIDE = "How to read the marks:"


def _section(report: str, heading: str) -> str:
    match = re.search(rf"^### {re.escape(heading)}\n(.*?)(?=^### |\Z)", report, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def _unquote(block: str) -> str:
    return "\n".join(re.sub(r"^> ?", "", line) for line in block.strip().splitlines())


def _load(path: Path) -> tuple[dict, str]:
    output = json.loads(path.read_text())
    draft = json.loads(path.with_name(path.name.replace(".output.", ".input.")).read_text())["text"]
    return output, draft


def _without_quoted_draft(report: str, draft: str) -> str:
    """The report minus the writer's own words, which keep their own punctuation."""
    for line in draft.splitlines():
        if line.strip():
            report = report.replace(line, "")
    report = re.sub(r"~~.*?~~", "", report)
    return re.sub(r'"[^"\n]*"', "", report)


def test_there_are_examples():
    assert {json.loads(p.read_text())["verdict"] for p in EXAMPLES} >= {"ready_to_post", "needs_rewrite"}


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_report_reads_as_skill_md_says(path):
    output, draft = _load(path)
    report = output["report_markdown"]
    assert report.startswith(f"## AI slop check\n\n**Verdict:** {VERDICTS[output['verdict']]}. ")
    assert not RULEBOOK_WORDS.search(_without_quoted_draft(report, draft))
    assert not OLD_NOTE_TAG.search(report)
    assert "—" not in _without_quoted_draft(report, draft)
    bullets = [line for line in _section(report, "Checked and left alone").splitlines() if line.startswith("- ")]
    assert len(bullets) == output["left_alone"]
    if not output["left_alone"]:
        assert "### Checked and left alone" not in report

    if output["marks"]:
        assert output["verdict"] != "ready_to_post"
        first = re.findall(r"^\d+\. ", _section(report, "Fix these first"), re.MULTILINE)
        assert 1 <= len(first) <= 3
        marked = _section(report, "Every change, marked in your draft")
        assert GUIDE in marked
        assert len(MARK.findall(marked)) + len(NOTE.findall(marked)) == output["marks"]
        assert "### Your draft\n" not in report
    else:
        assert output["verdict"] == "ready_to_post"
        for heading in ("Fix these first", "Before and after", "Every change, marked in your draft"):
            assert f"### {heading}" not in report
        assert "~~" not in report and GUIDE not in report


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_the_draft_comes_back_verbatim(path):
    output, draft = _load(path)
    report = output["report_markdown"]
    if output["marks"]:
        before = _section(report, "Before and after").split("**Your draft**", 1)[1].split("**With the cuts made**")[0]
        assert _unquote(before) == draft.strip()
        marked = _section(report, "Every change, marked in your draft").split("\n---\n\n", 1)[1]
        unmarked = NOTE.sub("", MARK.sub(lambda m: m.group(1), marked))
        assert unmarked.strip() == draft.strip()
    else:
        assert _unquote(_section(report, "Your draft")) == draft.strip()


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_fixes_and_clean_copy_invent_no_numbers(path):
    output, draft = _load(path)
    report = output["report_markdown"]
    draft_numbers = set(re.findall(r"\d+", draft))
    fixes = " ".join(fix for _, fix, _ in MARK.findall(report))
    clean = _section(report, "Before and after").split("**With the cuts made**")[-1] if output["marks"] else ""
    assert set(re.findall(r"\d+", fixes + clean)) <= draft_numbers
    assert "~~" not in clean


def test_prd_example_marks_each_single_empty_tell():
    output, _ = _load(SKILL / "assets" / "examples" / "prd-example.output.json")
    patterns = [reason.split(":")[0] for _, _, reason in MARK.findall(output["report_markdown"])]
    assert patterns == ["false contrast", "filler transition", "inflated word", "rule of three", "summary ending"]
    assert output["verdict"] == "needs_rewrite"


def _normalized(text: str) -> str:
    text = re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"[ \t]+", " ", text))
    return "\n".join(line.strip() for line in text.lower().splitlines()).strip()


@pytest.mark.parametrize("path", [p for p in EXAMPLES if json.loads(p.read_text())["marks"]], ids=lambda p: p.stem)
def test_clean_copy_is_built_from_the_marks(path):
    output, _ = _load(path)
    report = output["report_markdown"]
    marked = _section(report, "Every change, marked in your draft").split("\n---\n\n", 1)[1]

    def apply(mark: re.Match) -> str:
        fix = mark.group(2)
        return "" if fix.lower().startswith("cut it") else fix

    expected = NOTE.sub("", MARK.sub(apply, marked))
    clean = _unquote(_section(report, "Before and after").split("**With the cuts made**")[1])
    assert _normalized(clean) == _normalized(expected)
    assert all(struck.strip() != fix.strip() for struck, fix, _ in MARK.findall(marked))
