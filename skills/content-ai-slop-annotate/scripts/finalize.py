#!/usr/bin/env python3
"""Check an annotated AI Slop Report against its draft, then save it as the run's file.

The model writes the fixes; this script checks what it wrote with the same detectors the
pre-scan uses, so the rules are enforced by code rather than by asking twice:

- text_changed: the draft, with the annotations removed, must equal the original exactly.
- fix_has_tell: no fix, note or reason may contain an em dash, a list of three, a word
  from #4 or a formulaic transition.
- label_format: every inline label is "#N name" and nothing else.
- counts_line: the header's Counts line is the pre-scan's.
- not_flagged / missing_flag: a category the pre-scan did not flag is never annotated
  inline (6 and 11 excepted), and every category it flagged is.

Standard library only. Prints one JSON object on stdout; writes only the saved report
under --out-dir, and removes the scratch files if they were put there.

    python3 scripts/finalize.py --original /tmp/draft.md --annotated /tmp/report.md \\
        --name "Slop-check" [--out-dir /workspace/out] [--no-save] [--accept]

Exit codes: 0 checks passed (and the report was saved unless --no-save); 1 problems were
found (nothing saved, unless --accept); 2 bad arguments or empty files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prescan as ps  # noqa: E402

DEFAULT_OUT_DIR = "/workspace/out"
SPAN_CATEGORIES = {1, 2, 3, 4, 5, 6, 9, 11, 12, 13}
BLOCK_CATEGORIES = {7, 8, 10}

INLINE = re.compile(
    r"~~(?P<old>(?:(?!~~).)+?)~~ → \*\*(?P<fix>(?:(?!\*\*|~~).)+?)\*\* "
    r"\*\((?P<label>#(?P<cat>\d+)[^)]*)\)\*",
    re.DOTALL,
)
NOTE_LINE = re.compile(r"^> \*\*SLOP #(?P<cat>\d+)\*\*(?P<text>.*)$", re.MULTILINE)
NOTE_BLOCK = re.compile(r"^> \*\*SLOP #\d+\*\*.*\n(?:[ \t]*\n)?", re.MULTILINE)
LABEL = re.compile(r"^#(\d+) [a-z][a-z -]{1,38}$")
QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”")
STRICT_INTENSIFIERS = [
    re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE)
    for pattern, needs_judgment in ps.INTENSIFIERS
    if not needs_judgment
]
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
# This skill's own scratch names: removed from the out dir even when not passed, because a
# copy written there by mistake would otherwise ship as a second run file.
SCRATCH_NAMES = ("content-ai-slop-annotate.md", "content-ai-slop-annotate.report.md")


def split_report(annotated: str) -> tuple[str, str] | None:
    """(header, body) around the first line that is exactly '---'."""
    m = re.search(r"^---[ \t]*$", annotated, re.MULTILINE)
    if not m:
        return None
    return annotated[: m.start()], annotated[m.end() :].lstrip("\n")


def strip_annotations(body: str) -> str:
    body = NOTE_BLOCK.sub("", body)
    return INLINE.sub(lambda m: m.group("old"), body)


def normalized(text: str) -> list[str]:
    """Lines with runs of spaces collapsed: spacing between annotations is not a change."""
    return [re.sub(r"[ \t]+", " ", line).strip() for line in text.strip().splitlines()]


def tells_in(phrase: str) -> list[str]:
    """The tells a fix or note itself contains, by name."""
    found = []
    if ps.EM_DASH.search(phrase):
        found.append("em dash")
    if any(ps.triplets_in(s) for s in ps.sentences_of(phrase)):
        found.append("list of three")
    words = [p.search(phrase).group(0) for p in STRICT_INTENSIFIERS if p.search(phrase)]
    if words:
        found.append("#4 word: " + ", ".join(sorted(set(w.lower() for w in words))))
    if ps.TRANSITION_OPENERS.match(phrase) or ps.TRANSITION_ANYWHERE.search(phrase):
        found.append("formulaic transition")
    return found


def check(original: str, annotated: str) -> list[dict]:
    problems: list[dict] = []

    def problem(kind: str, detail: str, text: str = "") -> None:
        problems.append({"check": kind, "detail": detail, "text": text[:300]})

    parts = split_report(annotated)
    if parts is None:
        problem("format", "no '---' line between the header and the annotated text")
        return problems
    header, body = parts

    scan = ps.prescan(original)
    if f"**Counts:** {scan['counts_line']}" not in header:
        problem("counts_line", f"the Counts line must be exactly: {scan['counts_line']}")

    restored, expected = normalized(strip_annotations(body)), normalized(original)
    if restored != expected:
        for i, (got, want) in enumerate(zip(restored + [""] * len(expected), expected)):
            if got != want:
                problem(
                    "text_changed",
                    f"line {i + 1} of the text differs from the original once the "
                    f"annotations are removed; keep every unflagged word, line and paragraph "
                    f"break as written, and strike only text you replace",
                    f"expected: {want!r} got: {got!r}",
                )
                break
        else:
            problem("text_changed", "the annotated text has extra lines the original lacks")

    inline_cats: set[int] = set()
    for m in INLINE.finditer(body):
        cat = int(m.group("cat"))
        inline_cats.add(cat)
        if not LABEL.match(m.group("label")):
            problem("label_format", 'a label must be "#N name" and nothing else', m.group("label"))
        tells = tells_in(m.group("fix"))
        if tells:
            problem("fix_has_tell", "the fix contains " + "; ".join(tells), m.group("fix"))

    note_cats: set[int] = set()
    for m in NOTE_LINE.finditer(body):
        note_cats.add(int(m.group("cat")))
        if ps.EM_DASH.search(m.group("text")):
            problem("fix_has_tell", "the note contains an em dash", m.group(0))

    for line in header.splitlines():
        if line.startswith(("**Verdict:**", "**Hits by category:**", "**Borderline")):
            if ps.EM_DASH.search(QUOTED.sub("", line)):
                problem("fix_has_tell", "the header contains an em dash outside a quote", line)

    flagged = {
        c["id"]
        for c in scan["categories"]
        if c["decision"] == "flag" and any(not i["needs_judgment"] for i in c["instances"])
    }
    allowed_inline = {c["id"] for c in scan["categories"] if c["decision"] == "flag"} | set(
        ps.MODEL_JUDGED
    )
    for cat in sorted(inline_cats - allowed_inline):
        problem(
            "not_flagged",
            f"#{cat} is annotated inline but the pre-scan did not flag it; list it under "
            f"Borderline instead",
        )
    for cat in sorted(flagged):
        present = note_cats if cat in BLOCK_CATEGORIES else inline_cats
        if cat not in present:
            where = "a > **SLOP #N** note" if cat in BLOCK_CATEGORIES else "an inline fix"
            problem("missing_flag", f"the pre-scan flagged #{cat}; it needs {where}")
    return problems


def save(annotated: str, name: str, out_dir: Path) -> dict:
    stem = SAFE_NAME.sub("-", name.strip()).strip("-.") or "Slop-check"
    stem = stem[:-3] if stem.lower().endswith(".md") else stem
    out_dir.mkdir(parents=True, exist_ok=True)
    path, n = out_dir / f"{stem}.md", 2
    while path.exists():
        path, n = out_dir / f"{stem}-{n}.md", n + 1
    data = annotated.encode("utf-8")
    path.write_bytes(data)
    return {
        "path": str(path),
        "file_name": path.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def remove_scratch(paths: list[Path], out_dir: Path) -> list[str]:
    """Scratch files belong in /tmp; any found under the out dir would become run files."""
    removed = []
    out = out_dir.resolve()
    for p in [*paths, *(out_dir / name for name in SCRATCH_NAMES)]:
        rp = p.resolve()
        if rp.is_file() and out in rp.parents:
            rp.unlink()
            removed.append(str(rp))
    return removed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--original", required=True, help="the draft, as given")
    parser.add_argument("--annotated", required=True, help="the annotated report")
    parser.add_argument("--name", default="Slop-check", help="file name to save under")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--no-save", action="store_true", help="check only")
    parser.add_argument("--accept", action="store_true", help="save even with problems")
    args = parser.parse_args(argv)

    original_path, annotated_path = Path(args.original), Path(args.annotated)
    try:
        original = original_path.read_text(encoding="utf-8")
        annotated = annotated_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(json.dumps({"error": "unreadable_input", "detail": str(exc)}), file=sys.stderr)
        return 2
    if not original.strip() or not annotated.strip():
        print(json.dumps({"error": "empty_input", "detail": "an input file is empty"}), file=sys.stderr)
        return 2

    problems = check(original, annotated)
    result: dict = {"ok": not problems, "problems": problems, "saved": None, "removed_scratch": []}
    if (not problems or args.accept) and not args.no_save:
        out_dir = Path(args.out_dir)
        result["removed_scratch"] = remove_scratch([original_path, annotated_path], out_dir)
        result["saved"] = save(annotated, args.name, out_dir)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
