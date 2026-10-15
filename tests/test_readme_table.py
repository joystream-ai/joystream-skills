"""The README skills table is generated from frontmatter (PLAN § D10, joystream-q80lo.16.2 -> .16.1).

Contract these tests pin for ``scripts/readme_table.py``:

* ``run(root: Path, check: bool) -> dict`` returns ``{"changed": bool, "skills": int}``.
  It rewrites the block between ``<!-- skills-table:start -->`` and
  ``<!-- skills-table:end -->`` in ``<root>/README.md`` from every ``skills/*/SKILL.md``
  (frontmatter ``name``, ``description``, ``metadata.version``, ``metadata.category``) and
  the marketplace entry that lists the skill; with ``check=True`` it changes nothing.
* Rows are sorted by name; the first column links ``./skills/<name>/SKILL.md``.
* CLI: ``uv run scripts/readme_table.py [--root DIR] [--check] [--json]`` prints the same
  dict with ``--json``; ``--check`` exits 1 when the table is out of date, else 0.

Run: uv run --with pytest --with pyyaml --with jsonschema pytest tests/test_readme_table.py
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "templates" / "skill-template"
SCRIPT = REPO / "scripts" / "readme_table.py"
START = "<!-- skills-table:start -->"
END = "<!-- skills-table:end -->"


def _readme_table():
    """Load the generator inside the test, so its absence is a failure, not a collection error."""
    spec = importlib.util.spec_from_file_location("readme_table", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["readme_table"] = module
    spec.loader.exec_module(module)
    return module


def write_marketplace(root: Path, entries: list[tuple[str, list[str]]]) -> None:
    (root / ".claude-plugin").mkdir(exist_ok=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": "test",
        "plugins": [{"name": n, "source": "./", "skills": skills} for n, skills in entries],
    }))


def add_skill(root: Path, name: str, *, description: str, version: str, category: str) -> None:
    """A copy of the template renamed to ``name``, with its own frontmatter values."""
    target = root / "skills" / name
    shutil.copytree(TEMPLATE, target)
    skill_md = target / "SKILL.md"
    text = skill_md.read_text()
    head, _, body = text.partition("\n---\n")
    head = head.replace("name: skill-template", f"name: {name}")
    head = head.replace('  version: "0.1.0"', f'  version: "{version}"')
    head = head.replace("  category: sales", f"  category: {category}")
    lines = [
        f"description: {description}" if line.startswith("description:") else line
        for line in head.splitlines()
    ]
    skill_md.write_text("\n".join(lines) + "\n---\n" + body)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """One registered skill and a README whose table block is empty."""
    add_skill(
        tmp_path,
        "account-demo-judge",
        description="Judges a demo account against an ICP. Use when qualifying accounts.",
        version="1.2.0",
        category="sales",
    )
    write_marketplace(tmp_path, [("sales-tools", ["./skills/account-demo-judge"])])
    (tmp_path / "README.md").write_text(
        "# Test skills\n\nIntro paragraph.\n\n## Skills\n\n"
        f"{START}\n{END}\n\n## License\n\nMIT\n"
    )
    return tmp_path


def table_block(root: Path) -> str:
    text = (root / "README.md").read_text()
    return text[text.index(START) + len(START) : text.index(END)]


def test_new_skill_appears_in_the_regenerated_table(repo: Path) -> None:
    readme_table = _readme_table()
    assert readme_table.run(repo, check=False) == {"changed": True, "skills": 1}
    block = table_block(repo)
    assert "[`account-demo-judge`](./skills/account-demo-judge/SKILL.md)" in block
    assert "Judges a demo account against an ICP. Use when qualifying accounts." in block
    assert "1.2.0" in block
    assert "sales-tools" in block

    add_skill(
        repo,
        "content-demo-draft",
        description="Drafts a demo post from a release. Use when announcing releases.",
        version="0.3.1",
        category="marketing",
    )
    write_marketplace(
        repo,
        [("sales-tools", ["./skills/account-demo-judge"]), ("content", ["./skills/content-demo-draft"])],
    )
    assert readme_table.run(repo, check=False) == {"changed": True, "skills": 2}
    block = table_block(repo)
    assert "[`content-demo-draft`](./skills/content-demo-draft/SKILL.md)" in block
    assert "0.3.1" in block
    assert "marketing" in block
    assert block.index("account-demo-judge") < block.index("content-demo-draft"), "sorted by name"


def test_regeneration_is_idempotent_and_keeps_the_rest_of_the_readme(repo: Path) -> None:
    readme_table = _readme_table()
    readme_table.run(repo, check=False)
    first = (repo / "README.md").read_text()
    assert readme_table.run(repo, check=False) == {"changed": False, "skills": 1}
    assert (repo / "README.md").read_text() == first
    assert first.startswith("# Test skills\n\nIntro paragraph.\n\n## Skills\n\n")
    assert first.endswith("\n\n## License\n\nMIT\n")


def test_check_reports_drift_without_writing(repo: Path) -> None:
    readme_table = _readme_table()
    before = (repo / "README.md").read_text()
    assert readme_table.run(repo, check=True) == {"changed": True, "skills": 1}
    assert (repo / "README.md").read_text() == before


def _cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--json", *args],
        capture_output=True, text=True,
    )


def test_cli_check_exits_1_on_drift_then_0_after_regeneration(repo: Path) -> None:
    drift = _cli(repo, "--check")
    assert drift.returncode == 1, drift.stderr
    assert json.loads(drift.stdout) == {"changed": True, "skills": 1}

    regen = _cli(repo)
    assert regen.returncode == 0, regen.stderr
    assert json.loads(regen.stdout) == {"changed": True, "skills": 1}

    clean = _cli(repo, "--check")
    assert clean.returncode == 0, clean.stderr
    assert json.loads(clean.stdout) == {"changed": False, "skills": 1}


def test_cli_fails_when_the_readme_has_no_markers(repo: Path) -> None:
    (repo / "README.md").write_text("# No table here\n")
    result = _cli(repo, "--check")
    assert result.returncode == 1
    assert START in result.stderr
    assert (repo / "README.md").read_text() == "# No table here\n"
