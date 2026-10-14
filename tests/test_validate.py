"""Each authoring rule fails on a copy of the template broken in exactly one way.

Run: uv run --with pytest --with pyyaml --with jsonschema pytest tests
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


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


validate = _load("validate")
sync_shared = _load("sync_shared")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal repo with the template installed as skills/skill-template and registered."""
    shutil.copytree(TEMPLATE, tmp_path / "skills" / "skill-template")
    write_marketplace(tmp_path, [("demo", ["./skills/skill-template"])])
    return tmp_path


def write_marketplace(root: Path, entries: list[tuple[str, list[str]]]) -> None:
    (root / ".claude-plugin").mkdir(exist_ok=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": "test",
        "plugins": [{"name": n, "source": "./", "skills": skills} for n, skills in entries],
    }))


def skill_md(repo: Path) -> Path:
    return repo / "skills" / "skill-template" / "SKILL.md"


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"fixture drift: {old!r} not in {path.name}"
    path.write_text(text.replace(old, new, 1))


def codes(repo: Path) -> set[str]:
    result = validate.validate_skill(repo / "skills" / "skill-template")
    return {f.code for f in result.findings if f.severity == "error"}


def repo_codes(repo: Path) -> set[str]:
    results = [validate.validate_skill(d) for d in sorted((repo / "skills").iterdir())]
    return {f.code for f in validate.check_repo(repo, results)}


def test_template_is_clean(repo: Path) -> None:
    assert codes(repo) == set()
    assert repo_codes(repo) == set()


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("license: MIT\n", "license: MIT\nskill_kind: instruction\n", "SCHEMA_SKILL_NONSTANDARD_KEY"),
        ("license: MIT\n", "license: MIT\nallowed_tools: Read\n", "SCHEMA_SKILL_NONSTANDARD_KEY"),
        ("license: MIT\n", "license: MIT\ninput_schema:\n  type: object\n", "SCHEMA_SKILL_NONSTANDARD_KEY"),
        ("  category: sales\n", "  category: sales\n  tags: [a, b]\n", "SCHEMA_SKILL_FRONTMATTER_FLOW_STYLE"),
        ("name: skill-template", "name: other-name", "SCHEMA_SKILL_NAME_MATCHES_DIR"),
        ("name: skill-template", "name: Skill--Template", "SCHEMA_SKILL_NAME_INVALID"),
        ('  version: "0.1.0"', "  version: 1", "SCHEMA_SKILL_METADATA_NOT_FLAT"),
        ('  version: "0.1.0"', '  version: "1.0"', "CATALOG_VERSION_NOT_SEMVER"),
        ("  author: joystream\n", "", "CATALOG_METADATA_REQUIRED"),
        ("  joystream-skill-kind: instruction", "  joystream-skill-kind: magic", "SCHEMA_SKILL_KIND_INVALID"),
        ("  joystream-skill-kind: instruction\n", "", "CATALOG_SKILL_KIND_MISSING"),
        ("This skill performs no external operations;", "This skill reads the evidence;", "SCHEMA_SKILL_EXTERNAL_OPS_UNSTATED"),
        ("joystream-input-schema: assets/input.schema.json", "joystream-input-schema: ../escape.json", "SCHEMA_SKILL_SCHEMA_FILE_INVALID"),
        ("joystream-input-schema: assets/input.schema.json", "joystream-input-schema: assets/missing.json", "SCHEMA_SKILL_SCHEMA_FILE_INVALID"),
        ("See the paired cases", "Read references/rubric.md first. See the paired cases", "SCHEMA_SKILL_DANGLING_REFERENCE"),
    ],
)
def test_frontmatter_and_body_rules(repo: Path, old: str, new: str, code: str) -> None:
    edit(skill_md(repo), old, new)
    assert code in codes(repo)


def test_description_too_long(repo: Path) -> None:
    text = skill_md(repo).read_text()
    start = text.index("description: ") + len("description: ")
    end = text.index("\n", start)
    skill_md(repo).write_text(text[:start] + "x" * 1025 + text[end:])
    assert "SCHEMA_SKILL_DESCRIPTION_REQUIRED" in codes(repo)


def test_body_too_long(repo: Path) -> None:
    skill_md(repo).write_text(skill_md(repo).read_text() + "\nline" * 501)
    assert "SCHEMA_SKILL_BODY_TOO_LONG" in codes(repo)


def test_scripts_need_sandbox_kind(repo: Path) -> None:
    scripts = repo / "skills" / "skill-template" / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text("print('hi')\n")
    assert "CATALOG_SCRIPTS_NEED_SANDBOX_KIND" in codes(repo)
    edit(skill_md(repo), "joystream-skill-kind: instruction", "joystream-skill-kind: sandbox")
    assert "CATALOG_SCRIPTS_NEED_SANDBOX_KIND" not in codes(repo)


def test_output_schema_must_be_object(repo: Path) -> None:
    path = repo / "skills" / "skill-template" / "assets" / "output.schema.json"
    path.write_text(json.dumps({"type": "array", "items": {}}))
    assert "SCHEMA_SKILL_OUTPUT_SCHEMA_NOT_OBJECT" in codes(repo)


def test_schema_must_be_valid_json_schema(repo: Path) -> None:
    path = repo / "skills" / "skill-template" / "assets" / "input.schema.json"
    path.write_text(json.dumps({"type": "not-a-type"}))
    assert "SCHEMA_SKILL_SCHEMA_FILE_INVALID" in codes(repo)


def test_example_must_match_schema(repo: Path) -> None:
    path = repo / "skills" / "skill-template" / "assets" / "examples" / "basic.input.json"
    path.write_text(json.dumps({"subject": ""}))
    assert "CATALOG_EXAMPLE_INVALID" in codes(repo)


def test_example_must_be_paired(repo: Path) -> None:
    (repo / "skills" / "skill-template" / "assets" / "examples" / "basic.output.json").unlink()
    assert "CATALOG_EXAMPLE_UNPAIRED" in codes(repo)


def test_examples_required_with_schemas(repo: Path) -> None:
    shutil.rmtree(repo / "skills" / "skill-template" / "assets" / "examples")
    assert "CATALOG_EXAMPLES_MISSING" in codes(repo)


def test_unregistered_skill(repo: Path) -> None:
    write_marketplace(repo, [("demo", [])])
    assert "CATALOG_MARKETPLACE_UNREGISTERED" in repo_codes(repo)


def test_skill_in_two_entries(repo: Path) -> None:
    write_marketplace(repo, [("a", ["./skills/skill-template"]), ("b", ["./skills/skill-template"])])
    assert "CATALOG_MARKETPLACE_DUPLICATE" in repo_codes(repo)


def test_dangling_marketplace_entry(repo: Path) -> None:
    write_marketplace(repo, [("demo", ["./skills/skill-template", "./skills/gone"])])
    assert "CATALOG_MARKETPLACE_DANGLING" in repo_codes(repo)


def test_duplicate_names(repo: Path) -> None:
    shutil.copytree(repo / "skills" / "skill-template", repo / "skills" / "copy")
    write_marketplace(repo, [("demo", ["./skills/skill-template", "./skills/copy"])])
    assert "CATALOG_NAME_DUPLICATE" in repo_codes(repo)


def _cli(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "validate.py"), "--root", str(repo), "--json", *args],
        capture_output=True, text=True,
    )


def test_baseline_suppresses_known_error_and_goes_stale(repo: Path) -> None:
    edit(skill_md(repo), '  version: "0.1.0"', '  version: "1.0"')
    assert _cli(repo).returncode == 1

    (repo / "validate-baseline.json").write_text(json.dumps({"skill-template": ["CATALOG_VERSION_NOT_SEMVER"]}))
    run = _cli(repo)
    assert run.returncode == 0, run.stdout
    assert json.loads(run.stdout)["skills"][0]["baselined"] == ["CATALOG_VERSION_NOT_SEMVER"]

    edit(skill_md(repo), '  version: "1.0"', '  version: "0.1.0"')
    run = _cli(repo)
    assert run.returncode == 1
    assert [e["rule_id"] for e in json.loads(run.stdout)["repo_errors"]] == ["CATALOG_BASELINE_STALE"]


def test_sync_detects_and_repairs_drift(repo: Path) -> None:
    # Point the sync at the temp repo's skills but the real shared files.
    roots = (repo / "skills",)
    assert sync_shared.run(check=True, skill_roots=roots, repo_root=repo) == []

    output = repo / "skills" / "skill-template" / "assets" / "output.schema.json"
    schema = json.loads(output.read_text())
    schema["properties"]["report"]["properties"]["title"]["maxLength"] = 999
    output.write_text(json.dumps(schema))
    edit(skill_md(repo), "### Reading the evidence", "### Reading evidence (edited)")

    drifted = sync_shared.run(check=True, skill_roots=roots, repo_root=repo)
    assert sorted(drifted) == ["skills/skill-template/SKILL.md", "skills/skill-template/assets/output.schema.json"]
    sync_shared.run(check=False, skill_roots=roots, repo_root=repo)
    assert sync_shared.run(check=True, skill_roots=roots, repo_root=repo) == []


def test_sync_fills_item_result_only_where_declared(repo: Path) -> None:
    roots = (repo / "skills",)
    output = repo / "skills" / "skill-template" / "assets" / "output.schema.json"
    schema = json.loads(output.read_text())
    assert "item_result" not in schema["properties"]

    schema["properties"]["item_result"] = {"type": "object"}
    output.write_text(json.dumps(schema))
    assert sync_shared.run(check=True, skill_roots=roots, repo_root=repo) == [
        "skills/skill-template/assets/output.schema.json"
    ]
    sync_shared.run(check=False, skill_roots=roots, repo_root=repo)
    synced = json.loads(output.read_text())["properties"]["item_result"]
    assert synced["properties"]["status"]["enum"] == ["ok", "skipped", "no_evidence"]
    assert "$schema" not in synced
    assert sync_shared.run(check=True, skill_roots=roots, repo_root=repo) == []


def _report_validator():
    import jsonschema

    return jsonschema.Draft202012Validator(json.loads((REPO / "schemas" / "report.schema.json").read_text()))


def _report(source: dict) -> dict:
    return {"title": "t", "summary": "", "severity": "info", "sections": [], "sources": [source], "unknowns": []}


@pytest.mark.parametrize(
    ("source", "ok"),
    [
        ({"id": "S1", "url": "https://example.com/a"}, True),
        ({"id": "S1", "record_ref": "apollo:person/5f2a"}, True),
        ({"id": "S1", "url": "https://example.com", "record_ref": "crustdata:company/42"}, True),
        ({"id": "S1"}, False),
        ({"id": "S1", "record_ref": "not a ref"}, False),
    ],
)
def test_source_needs_url_or_record_ref(source: dict, ok: bool) -> None:
    assert _report_validator().is_valid(_report(source)) is ok


def test_sync_fills_state_out_only_where_declared(repo: Path) -> None:
    roots = (repo / "skills",)
    output = repo / "skills" / "skill-template" / "assets" / "output.schema.json"
    schema = json.loads(output.read_text())
    schema["properties"]["state_out"] = {}
    output.write_text(json.dumps(schema))
    sync_shared.run(check=False, skill_roots=roots, repo_root=repo)
    synced = json.loads(output.read_text())["properties"]["state_out"]
    assert synced["properties"]["entries"]["items"]["required"] == ["item_key", "fingerprint", "observed_at"]


def test_every_rule_has_an_impact_and_a_fix() -> None:
    import re as _re

    source = (REPO / "scripts" / "validate.py").read_text()
    emitted = set(_re.findall(r'"((?:SCHEMA_SKILL|CATALOG)_[A-Z_]+)"', source.split("RULES: dict", 1)[1].split("}\n", 1)[1]))
    assert len(emitted) >= 30, emitted
    assert emitted <= set(validate.RULES), emitted - set(validate.RULES)
    for rule_id, (impact, fix, autofix) in validate.RULES.items():
        assert impact in {"blocks", "degrades", "portability", "catalog"}, rule_id
        assert fix and isinstance(autofix, bool), rule_id
        assert rule_id.startswith("CATALOG_") == (impact == "catalog"), rule_id


def test_json_finding_explains_itself(repo: Path) -> None:
    edit(skill_md(repo), "license: MIT\n", "license: MIT\nskill_kind: instruction\n")
    run = _cli(repo)
    finding = json.loads(run.stdout)["skills"][0]["errors"][0]
    assert finding["rule_id"] == "SCHEMA_SKILL_NONSTANDARD_KEY"
    assert finding["impact"] == "portability"
    assert finding["autofix"] is True and finding["fix"]
