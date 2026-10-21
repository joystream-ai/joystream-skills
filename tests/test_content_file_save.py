"""content-file-save's script saves content unchanged, safely named, and refuses bad input.

Run: uv run --with pytest --with pyyaml --with jsonschema pytest tests
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

SKILL = Path(__file__).resolve().parent.parent / "skills" / "content-file-save"
SCRIPT = SKILL / "scripts" / "save_file.py"
OUTPUT_SCHEMA = json.loads((SKILL / "assets" / "output.schema.json").read_text())


def run(tmp_path: Path, content: str | bytes, *args: str) -> tuple[int, dict, Path]:
    source = tmp_path / "content"
    source.write_bytes(content if isinstance(content, bytes) else content.encode())
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(source), "--out-dir", str(out), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1, f"stdout must be exactly one JSON line, got {proc.stdout!r}"
    return proc.returncode, json.loads(lines[0]), out


def test_saves_markdown_unchanged(tmp_path: Path) -> None:
    report = "# Report\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    code, result, out = run(tmp_path, report, "--name", "Account discovery report")

    assert code == 0
    assert result["saved"] is True
    saved = out / "Account-discovery-report.md"
    assert result["path"] == str(saved)
    assert saved.read_text() == report
    assert result["sha256"] == hashlib.sha256(report.encode()).hexdigest()
    assert result["bytes"] == len(report.encode())
    assert result["media_type"] == "text/markdown"


def test_script_fields_fit_the_output_schema(tmp_path: Path) -> None:
    """The model copies the script's fields into its answer; they must all be allowed."""
    _, result, _ = run(tmp_path, "# Report\n")
    allowed = set(OUTPUT_SCHEMA["properties"])
    assert set(result) <= allowed, f"fields the schema does not allow: {set(result) - allowed}"


@pytest.mark.parametrize(
    ("name", "file_name"),
    [
        ("../../etc/passwd", "etc-passwd.md"),
        ("report.md", "report.md"),
        ('a:b*c?"d<e>f|g', "a-b-c-d-e-f-g.md"),
        ("", "document.md"),
        ("   ", "document.md"),
    ],
)
def test_names_are_made_safe(tmp_path: Path, name: str, file_name: str) -> None:
    _, result, out = run(tmp_path, "# Report\n", "--name", name)
    assert result["file_name"] == file_name
    assert Path(result["path"]).parent == out


def test_never_replaces_an_existing_file(tmp_path: Path) -> None:
    run(tmp_path, "# First\n", "--name", "report")
    _, second, out = run(tmp_path, "# Second\n", "--name", "report")

    assert second["file_name"] == "report-2.md"
    assert (out / "report.md").read_text() == "# First\n"


def test_overwrite_replaces_when_asked(tmp_path: Path) -> None:
    run(tmp_path, "# First\n", "--name", "report")
    _, result, out = run(tmp_path, "# Second\n", "--name", "report", "--overwrite")

    assert result["file_name"] == "report.md"
    assert (out / "report.md").read_text() == "# Second\n"


@pytest.mark.parametrize(
    ("content", "fmt", "error"),
    [
        ("", "md", "empty_content"),
        ("  \n\n", "txt", "empty_content"),
        ("{not json", "json", "invalid_json"),
        ("a,b\n1,2,3\n", "csv", "invalid_csv"),
        ("%PDF-1.7", "pdf", "unsupported_format"),
        (b"\xff\xfe\x00bad", "txt", "not_utf8"),
    ],
)
def test_refuses_bad_content(tmp_path: Path, content: str | bytes, fmt: str, error: str) -> None:
    code, result, out = run(tmp_path, content, "--format", fmt)

    assert code == 1
    assert result == {"saved": False, "error": error, "detail": result["detail"]}
    assert not out.exists() or not any(out.iterdir())


def test_accepts_valid_csv_and_json(tmp_path: Path) -> None:
    _, csv_result, _ = run(tmp_path, "rank,account\n1,Example Cloud\n", "--format", "csv", "--name", "t")
    _, json_result, _ = run(tmp_path, '{"top": ["example.com"]}', "--format", "json", "--name", "t")

    assert (csv_result["saved"], csv_result["file_name"]) == (True, "t.csv")
    assert (json_result["saved"], json_result["media_type"]) == (True, "application/json")


def test_examples_match_the_output_schema() -> None:
    for path in sorted((SKILL / "assets" / "examples").glob("*.output.json")):
        jsonschema.validate(json.loads(path.read_text()), OUTPUT_SCHEMA)
