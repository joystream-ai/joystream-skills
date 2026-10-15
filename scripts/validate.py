#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6", "jsonschema>=4.21"]
# ///
"""Check every skill against the Agent Skills spec and this repo's authoring standard.

The official `skills-ref validate` runs separately in CI. This adds what it does not check:
the strict six-key rule, unique names, marketplace registration, the JoyStream metadata
keys, JSON Schema contracts and examples, size budgets, and the body rules in
CONTRIBUTING.md.

    uv run scripts/validate.py                       # every skill under skills/
    uv run scripts/validate.py skills/foo --json     # one skill, machine-readable

Exit 1 when any error is not covered by validate-baseline.json, or when the baseline
lists an error that no longer occurs (the baseline only ever shrinks).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

STANDARD_KEYS = frozenset({"name", "description", "license", "compatibility", "metadata", "allowed-tools"})
SKILL_KINDS = frozenset({"instruction", "task", "tool", "sandbox"})
REQUIRED_METADATA = ("author", "version")
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
SCHEMA_POINTERS = {
    "joystream-input-schema": "assets/input.schema.json",
    "joystream-output-schema": "assets/output.schema.json",
}
NO_EXTERNAL_OPS = "performs no external operations"
MAX_BODY_LINES = 500
MAX_BODY_TOKENS = 5000  # spec guidance; approximated as chars / 4
# Paths in the body that point into the skill's own folders.
BODY_REF_RE = re.compile(r"(?<![\w/.-])((?:references|assets|scripts)/[\w./-]+)")
FLOW_STYLE_RE = re.compile(r":\s*[\[{]")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

# One entry per rule: (impact, how to fix, mechanically fixable).
# SCHEMA_SKILL_* ids are the ones JoyStream's importer records too (same id, same meaning);
# CATALOG_* ids are this repo's house rules. Impact says what the finding costs the user:
#   blocks       the skill will not load or run correctly on JoyStream
#   degrades     it runs, but loses validation, chaining or correct planning
#   portability  fine on JoyStream; claude.ai, the Skills API or skills-ref reject it
#   catalog      a rule of this repo only
RULES: dict[str, tuple[str, str, bool]] = {
    "SCHEMA_SKILL_MD_MISSING": ("blocks", "Add a SKILL.md at the root of the skill folder.", False),
    "SCHEMA_SKILL_FRONTMATTER_UNPARSEABLE": ("blocks", "Start SKILL.md with a --- block of valid YAML and close it with ---.", False),
    "SCHEMA_SKILL_NAME_INVALID": ("blocks", "Use 1-64 characters of a-z, 0-9 and single hyphens.", True),
    "SCHEMA_SKILL_NAME_MATCHES_DIR": ("blocks", "Make name equal the folder name (rename one of them).", True),
    "SCHEMA_SKILL_DESCRIPTION_REQUIRED": ("blocks", "Write a description of up to 1024 characters: what it does and when to use it.", False),
    "SCHEMA_SKILL_SCHEMA_FILE_INVALID": ("blocks", "Point metadata.joystream-*-schema at a JSON Schema 2020-12 file inside the skill, normally assets/*.schema.json.", False),
    "SCHEMA_SKILL_OUTPUT_SCHEMA_NOT_OBJECT": ("blocks", "Make the output schema type: object with properties; they become the fields of the model's answer.", False),
    "SCHEMA_SKILL_KIND_INVALID": ("blocks", "Set metadata.joystream-skill-kind to instruction, task, tool or sandbox.", False),
    "SCHEMA_SKILL_DANGLING_REFERENCE": ("degrades", "Add the referenced file, or remove the reference from the body.", False),
    "SCHEMA_SKILL_EXTERNAL_OPS_UNSTATED": ("degrades", "Say in the body that the skill 'performs no external operations;' and name the earlier steps that supply its evidence.", False),
    "SCHEMA_SKILL_BODY_TOO_LONG": ("degrades", "Keep SKILL.md under 500 lines; move optional depth to references/.", False),
    "SCHEMA_SKILL_BODY_TOKEN_BUDGET": ("degrades", "Keep the body near 5,000 tokens; move optional depth to references/.", False),
    "SCHEMA_SKILL_METADATA_NOT_FLAT": ("portability", "Make metadata map strings to strings; quote numbers such as \"1.0.0\".", True),
    "SCHEMA_SKILL_NONSTANDARD_KEY": ("portability", "Move the key under metadata (joystream-skill-kind) or its content into assets/ (schemas), or rename allowed_tools to allowed-tools.", True),
    "SCHEMA_SKILL_FRONTMATTER_FLOW_STYLE": ("portability", "Write lists and maps in block style, one item per line.", True),
    "SCHEMA_SKILL_COMPATIBILITY_TOO_LONG": ("portability", "Make compatibility a string of 1-500 characters, or remove it.", False),
    "SCHEMA_SKILL_ALLOWED_TOOLS_INVALID": ("portability", "Write allowed-tools as one space-separated string.", True),
    "SCHEMA_SKILL_REFERENCE_TOO_DEEP": ("portability", "Reference files one level deep from SKILL.md, e.g. references/rubric.md.", False),
    "CATALOG_SKILL_KIND_MISSING": ("catalog", "Add metadata.joystream-skill-kind.", True),
    "CATALOG_METADATA_REQUIRED": ("catalog", "Add metadata.author and metadata.version.", False),
    "CATALOG_VERSION_NOT_SEMVER": ("catalog", "Use a quoted semver version, e.g. \"1.0.0\".", True),
    "CATALOG_SCHEMA_POINTER_NONDEFAULT": ("catalog", "Keep schemas at assets/input.schema.json and assets/output.schema.json.", True),
    "CATALOG_SCRIPTS_NEED_SANDBOX_KIND": ("catalog", "Remove scripts/, or mark the skill joystream-skill-kind: sandbox.", False),
    "CATALOG_EXAMPLES_MISSING": ("catalog", "Add at least one assets/examples/<case>.input.json and .output.json pair.", False),
    "CATALOG_EXAMPLE_INVALID": ("catalog", "Make the example match its schema.", False),
    "CATALOG_EXAMPLE_UNPAIRED": ("catalog", "Give each example both an .input.json and an .output.json.", False),
    "CATALOG_EXAMPLE_MISNAMED": ("catalog", "Name examples <case>.input.json and <case>.output.json.", True),
    "CATALOG_NAME_DUPLICATE": ("catalog", "Give each skill a unique name; JoyStream resolves skills by bare name.", False),
    "CATALOG_MARKETPLACE_MISSING": ("catalog", "Add .claude-plugin/marketplace.json.", False),
    "CATALOG_MARKETPLACE_DANGLING": ("catalog", "Remove the entry, or add the skill it points to.", False),
    "CATALOG_MARKETPLACE_UNREGISTERED": ("catalog", "Add the skill to exactly one marketplace entry.", False),
    "CATALOG_MARKETPLACE_DUPLICATE": ("catalog", "Keep the skill in exactly one marketplace entry.", False),
    "CATALOG_BASELINE_STALE": ("catalog", "Remove the entry from validate-baseline.json.", True),
}


@dataclass
class Finding:
    code: str
    message: str
    severity: str = "error"  # error | warning

    def as_dict(self) -> dict:
        impact, fix, autofix = RULES[self.code]
        return {
            "rule_id": self.code,
            "severity": self.severity,
            "impact": impact,
            "detail": self.message,
            "fix": fix,
            "autofix": autofix,
        }


@dataclass
class SkillResult:
    path: str
    name: str | None = None
    findings: list[Finding] = field(default_factory=list)
    baselined: list[str] = field(default_factory=list)

    def error(self, code: str, message: str) -> None:
        self.findings.append(Finding(code, message))

    def warn(self, code: str, message: str) -> None:
        self.findings.append(Finding(code, message, "warning"))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error" and f.code not in self.baselined]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]


def split_frontmatter(text: str) -> tuple[str, str] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    after = text.find("\n", end + 4)
    body = "" if after == -1 else text[after + 1 :]
    return text[4:end], body


def inside(skill_dir: Path, rel: str) -> Path | None:
    """Resolve rel against skill_dir; None when it escapes the directory."""
    if rel.startswith("/") or "\\" in rel:
        return None
    resolved = (skill_dir / rel).resolve()
    try:
        resolved.relative_to(skill_dir.resolve())
    except ValueError:
        return None
    return resolved


def load_schema(result: SkillResult, path: Path, label: str) -> dict | None:
    try:
        schema = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        result.error("SCHEMA_SKILL_SCHEMA_FILE_INVALID", f"{label}: not readable JSON ({exc})")
        return None
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        result.error("SCHEMA_SKILL_SCHEMA_FILE_INVALID", f"{label}: not a valid JSON Schema 2020-12 ({exc.message})")
        return None
    return schema


def check_frontmatter(result: SkillResult, skill_dir: Path, raw: str) -> dict | None:
    for lineno, line in enumerate(raw.splitlines(), start=2):
        if FLOW_STYLE_RE.search(line) and not line.lstrip().startswith("#"):
            result.error(
                "SCHEMA_SKILL_FRONTMATTER_FLOW_STYLE",
                f"line {lineno}: inline [..] or {{..}} values are rejected by the reference "
                "validator (strictyaml); use block style",
            )
            break
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        result.error("SCHEMA_SKILL_FRONTMATTER_UNPARSEABLE", str(exc))
        return None
    if not isinstance(data, dict):
        result.error("SCHEMA_SKILL_FRONTMATTER_UNPARSEABLE", "frontmatter is not a mapping")
        return None

    for key in sorted(set(data) - STANDARD_KEYS):
        result.error(
            "SCHEMA_SKILL_NONSTANDARD_KEY",
            f"'{key}' is not one of the six Agent Skills keys; claude.ai and the Skills API "
            "reject it. Move it under metadata or into assets/",
        )

    name = data.get("name")
    result.name = name if isinstance(name, str) else None
    if not isinstance(name, str) or not name:
        result.error("SCHEMA_SKILL_NAME_INVALID", "name is required")
    else:
        if len(name) > 64 or not NAME_RE.match(name):
            result.error("SCHEMA_SKILL_NAME_INVALID", f"'{name}': 1-64 chars, a-z 0-9 and single hyphens, no leading/trailing hyphen")
        if name != skill_dir.name:
            result.error("SCHEMA_SKILL_NAME_MATCHES_DIR", f"name '{name}' must equal its directory '{skill_dir.name}'")

    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        result.error("SCHEMA_SKILL_DESCRIPTION_REQUIRED", "description is required")
    elif len(description) > 1024:
        result.error("SCHEMA_SKILL_DESCRIPTION_REQUIRED", f"{len(description)} chars; max 1024")

    compatibility = data.get("compatibility")
    if compatibility is not None and (not isinstance(compatibility, str) or not 1 <= len(compatibility) <= 500):
        result.error("SCHEMA_SKILL_COMPATIBILITY_TOO_LONG", "compatibility must be a 1-500 char string when present")

    allowed_tools = data.get("allowed-tools")
    if allowed_tools is not None and not isinstance(allowed_tools, str):
        result.error("SCHEMA_SKILL_ALLOWED_TOOLS_INVALID", "allowed-tools must be one space-separated string")

    metadata = data.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
    ):
        result.error("SCHEMA_SKILL_METADATA_NOT_FLAT", "metadata must map strings to strings (quote numbers like \"1.0.0\")")
        return data

    for key in REQUIRED_METADATA:
        if not metadata.get(key):
            result.error("CATALOG_METADATA_REQUIRED", f"metadata.{key} is required")
    version = metadata.get("version")
    if version and not SEMVER_RE.match(version):
        result.error("CATALOG_VERSION_NOT_SEMVER", f"metadata.version '{version}' must be semver, e.g. \"1.0.0\"")
    kind = metadata.get("joystream-skill-kind")
    if kind is None:
        result.error("CATALOG_SKILL_KIND_MISSING", "metadata.joystream-skill-kind is required (instruction | task | tool | sandbox)")
    elif kind not in SKILL_KINDS:
        result.error("SCHEMA_SKILL_KIND_INVALID", f"metadata.joystream-skill-kind '{kind}' not in {sorted(SKILL_KINDS)}")
    return data


def check_contract(result: SkillResult, skill_dir: Path, metadata: dict) -> None:
    schemas: dict[str, dict] = {}
    for key, default in SCHEMA_POINTERS.items():
        rel = metadata.get(key)
        if rel is None:
            continue
        target = inside(skill_dir, rel)
        if target is None:
            result.error("SCHEMA_SKILL_SCHEMA_FILE_INVALID", f"metadata.{key} '{rel}' leaves the skill directory")
            continue
        if not target.is_file():
            result.error("SCHEMA_SKILL_SCHEMA_FILE_INVALID", f"metadata.{key} '{rel}' does not exist")
            continue
        if rel != default:
            result.warn("CATALOG_SCHEMA_POINTER_NONDEFAULT", f"metadata.{key} is '{rel}'; the convention is '{default}'")
        schema = load_schema(result, target, rel)
        if schema is not None:
            schemas[key] = schema

    output = schemas.get("joystream-output-schema")
    if output is not None and (output.get("type") != "object" or not isinstance(output.get("properties"), dict)):
        result.error(
            "SCHEMA_SKILL_OUTPUT_SCHEMA_NOT_OBJECT",
            "output schema must be type: object with properties; they become the emit_result tool's fields",
        )

    examples_dir = skill_dir / "assets" / "examples"
    if not examples_dir.is_dir():
        if schemas:
            result.error("CATALOG_EXAMPLES_MISSING", "a skill with a schema needs at least one assets/examples/<case>.input.json/.output.json pair")
        return
    validators = {
        "input": schemas.get("joystream-input-schema"),
        "output": schemas.get("joystream-output-schema"),
    }
    cases: dict[str, set[str]] = {}
    for path in sorted(examples_dir.glob("*.json")):
        match = re.match(r"^(.+)\.(input|output)\.json$", path.name)
        if not match:
            result.error("CATALOG_EXAMPLE_MISNAMED", f"{path.name}: name examples <case>.input.json / <case>.output.json")
            continue
        case, side = match.groups()
        cases.setdefault(case, set()).add(side)
        schema = validators[side]
        if schema is None:
            continue
        try:
            instance = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            result.error("CATALOG_EXAMPLE_INVALID", f"{path.name}: not JSON ({exc})")
            continue
        for err in sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda e: list(e.path))[:5]:
            where = "/".join(str(p) for p in err.path) or "(root)"
            result.error("CATALOG_EXAMPLE_INVALID", f"{path.name} at {where}: {err.message}")
    for case, sides in sorted(cases.items()):
        if sides != {"input", "output"}:
            result.error("CATALOG_EXAMPLE_UNPAIRED", f"example '{case}' needs both .input.json and .output.json")


def check_body(result: SkillResult, skill_dir: Path, body: str, metadata: dict) -> None:
    lines = body.count("\n") + 1
    if lines > MAX_BODY_LINES:
        result.error("SCHEMA_SKILL_BODY_TOO_LONG", f"{lines} lines; keep SKILL.md under {MAX_BODY_LINES} and move depth to references/")
    tokens = len(body) // 4
    if tokens > MAX_BODY_TOKENS:
        result.warn("SCHEMA_SKILL_BODY_TOKEN_BUDGET", f"~{tokens} tokens; the spec recommends under {MAX_BODY_TOKENS}")

    # Only instruction skills run on JoyStream's tool-less llm leaf; task skills may call tools.
    if metadata.get("joystream-skill-kind") == "instruction" and NO_EXTERNAL_OPS not in body:
        result.error(
            "SCHEMA_SKILL_EXTERNAL_OPS_UNSTATED",
            f"an instruction skill's body must say it '{NO_EXTERNAL_OPS}' and name the prior steps that supply its evidence",
        )

    # HTML comments (e.g. the shared-block markers) are notes for authors, not file references.
    for ref in sorted(set(BODY_REF_RE.findall(HTML_COMMENT_RE.sub("", body)))):
        ref = ref.rstrip(".,)`'\"")
        target = inside(skill_dir, ref)
        if target is None or not target.exists():
            result.error("SCHEMA_SKILL_DANGLING_REFERENCE", f"body references '{ref}', which is not in the skill")
        elif ref.count("/") > 1 and not target.is_dir():
            result.warn("SCHEMA_SKILL_REFERENCE_TOO_DEEP", f"'{ref}' is two levels deep; the spec asks for one level from SKILL.md")


def check_files(result: SkillResult, skill_dir: Path, metadata: dict) -> None:
    if (skill_dir / "scripts").exists() and metadata.get("joystream-skill-kind") != "sandbox":
        result.error(
            "CATALOG_SCRIPTS_NEED_SANDBOX_KIND",
            "any file under scripts/ sends the step to JoyStream's sandbox; only joystream-skill-kind: sandbox may ship scripts",
        )


def validate_skill(skill_dir: Path) -> SkillResult:
    result = SkillResult(path=str(skill_dir))
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        result.error("SCHEMA_SKILL_MD_MISSING", "no SKILL.md")
        return result
    parts = split_frontmatter(skill_md.read_text())
    if parts is None:
        result.error("SCHEMA_SKILL_FRONTMATTER_UNPARSEABLE", "SKILL.md must start with --- frontmatter ---")
        return result
    raw, body = parts
    data = check_frontmatter(result, skill_dir, raw)
    metadata = data.get("metadata") if isinstance(data, dict) else None
    metadata = metadata if isinstance(metadata, dict) and all(isinstance(v, str) for v in metadata.values()) else {}
    check_contract(result, skill_dir, metadata)
    check_body(result, skill_dir, body, metadata)
    check_files(result, skill_dir, metadata)
    return result


def check_repo(root: Path, results: list[SkillResult]) -> list[Finding]:
    findings: list[Finding] = []
    seen: dict[str, str] = {}
    for result in results:
        if result.name and result.name in seen:
            findings.append(Finding("CATALOG_NAME_DUPLICATE", f"'{result.name}' in {seen[result.name]} and {result.path}"))
        elif result.name:
            seen[result.name] = result.path

    manifest = root / ".claude-plugin" / "marketplace.json"
    if not manifest.is_file():
        findings.append(Finding("CATALOG_MARKETPLACE_MISSING", ".claude-plugin/marketplace.json not found"))
        return findings
    entries = json.loads(manifest.read_text()).get("plugins", [])
    registered: dict[Path, list[str]] = {}
    for entry in entries:
        source = root / entry.get("source", "./")
        for rel in entry.get("skills", []):
            path = (source / rel).resolve()
            if not (path / "SKILL.md").is_file():
                findings.append(Finding("CATALOG_MARKETPLACE_DANGLING", f"{entry.get('name')}: '{rel}' has no SKILL.md"))
            registered.setdefault(path, []).append(entry.get("name", "?"))
    for result in results:
        owners = registered.get(Path(result.path).resolve(), [])
        if not owners:
            findings.append(Finding("CATALOG_MARKETPLACE_UNREGISTERED", f"{result.path} is in no marketplace entry, so the importer never sees it"))
        elif len(owners) > 1:
            findings.append(Finding(
                "CATALOG_MARKETPLACE_DUPLICATE",
                f"{result.path} is in {owners}; one skill, one entry (the platform resolves skills by bare name)",
            ))
    return findings


def apply_baseline(results: list[SkillResult], baseline: dict[str, list[str]]) -> list[Finding]:
    stale: list[Finding] = []
    by_name = {r.name: r for r in results if r.name}
    for name, codes in baseline.items():
        result = by_name.get(name)
        present = {f.code for f in result.findings if f.severity == "error"} if result else set()
        for code in codes:
            if code in present:
                result.baselined.append(code)
            else:
                stale.append(Finding("CATALOG_BASELINE_STALE", f"validate-baseline.json lists {name}:{code}, which no longer occurs; remove it"))
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="skill directories (default: every directory under skills/)")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent), help="repo root")
    parser.add_argument("--baseline", help="baseline file (default: <root>/validate-baseline.json)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    root = Path(args.root)
    all_dirs = sorted(p for p in (root / "skills").iterdir() if p.is_dir()) if (root / "skills").is_dir() else []
    dirs = [Path(p) for p in args.paths] if args.paths else all_dirs
    results = [validate_skill(d) for d in dirs]
    # Repo-level checks always see every skill, so a partial run still catches duplicates.
    repo_results = results if not args.paths else [validate_skill(d) for d in all_dirs]
    repo_findings = check_repo(root, repo_results)

    baseline_path = Path(args.baseline) if args.baseline else root / "validate-baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.is_file() else {}
    if args.paths:
        names = {r.name for r in results}
        baseline = {k: v for k, v in baseline.items() if k in names}
    repo_findings += apply_baseline(results, baseline)

    failed = bool(repo_findings) or any(r.errors for r in results)
    if args.json:
        print(json.dumps({
            "ok": not failed,
            "skills": [
                {
                    "name": r.name,
                    "path": r.path,
                    "errors": [f.as_dict() for f in r.errors],
                    "warnings": [f.as_dict() for f in r.warnings],
                    "baselined": sorted(set(r.baselined)),
                }
                for r in results
            ],
            "repo_errors": [f.as_dict() for f in repo_findings],
        }, indent=2))
    else:
        for r in results:
            status = "FAIL" if r.errors else "ok"
            extra = f" (baselined: {', '.join(sorted(set(r.baselined)))})" if r.baselined else ""
            print(f"{status:4} {r.path}{extra}")
            for f in r.errors + r.warnings:
                print(f"     {f.severity} [{RULES[f.code][0]}] {f.code}: {f.message}")
                print(f"       fix: {RULES[f.code][1]}")
        for f in repo_findings:
            print(f"repo error: {f.code}: {f.message}")
            print(f"  fix: {RULES[f.code][1]}")
        print("FAILED" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
