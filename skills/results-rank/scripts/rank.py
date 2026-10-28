#!/usr/bin/env python3
"""Rank the per-item results of a fan-out agent deterministically.

Run inside JoyStream's sandbox by the results-rank skill, in one command:

    python3 scripts/rank.py                       # v2: branches bound as an input
    python3 scripts/rank.py --steps /tmp/results-rank-steps.json   # v1: step outputs

Inputs come from the step's environment: INPUT_JSON (all inputs as one JSON object) or,
when that is over 64 KiB, the file named by INPUT_JSON_PATH. `--input FILE` reads them
from a file instead (tests, local runs).

Standard library only. Prints exactly one JSON object on stdout (the skill's output,
valid against assets/output.schema.json) and writes the same object to
/tmp/results-rank.json. On bad input it still prints a valid output object whose report
explains the error, and exits 1. Progress and warnings go to stderr.

What it does, in order (the SKILL.md summarizes the same rules):

1. Groups every branch as rankable, skipped or failed, and finds each branch's judge
   output (the output that holds an `item_result` object).
2. Recomputes score, verdict and tier for judges that report an
   account-icp-qualification style rubric (`rubric_used.criteria` with kind, weight and
   result, on a 0-5 scale), and records every difference in `corrections`. Every other
   judge's item_result is copied unchanged: nothing is ever re-judged from evidence.
3. Sorts by `rank_by`, then item_label (case-insensitive), then branch order.
4. Renumbers the branches' sources into one list and builds the report.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

SKILL = "results-rank"
VERSION = "1.6.0"
DEFAULT_OUT = "/tmp/results-rank.json"

MAX_RANKED = 100
MAX_FAILED = 100
MAX_SOURCES = 60
MAX_SECTIONS = 12
MAX_ITEMS = 25
MAX_UNKNOWNS = 20
NOT_JUDGED_SHOWN = 24

KEY_RE = re.compile(r"^[a-z_]+:[^@\s]+$")
RECORD_REF_RE = re.compile(r"^[a-z0-9_]+:[^\s]+$")
SOURCE_ID_RE = re.compile(r"^S[0-9]+$")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\+?\d[\d ()./-]{7,}\d")

# A line "reads as an instruction" when it opens a sentence with a command verb, or
# carries a classic injection phrase anywhere. Deliberately narrow: a false positive only
# hides a headline from the report (ranked[].headline keeps it).
COMMAND_START_RE = re.compile(
    r"(?:^|[.!?:;]\s+)(?:please\s+)?(?:"
    r"(?:ignore|disregard|forget|override)\b"
    r"|(?:rank|post|send|click|reply|forward|email|share|delete|run|execute|call|message|dm|approve)"
    r"\s+(?:this|it|me|us|them|the|that|all|here|to|now|immediately|everyone)\b)",
    re.IGNORECASE,
)
INJECTION_RE = re.compile(
    r"\b(?:ignore|disregard|forget)\b[^.]{0,40}\b(?:instructions?|rules|prompt|above|previous)\b"
    r"|\b(?:you are now|system prompt)\b"
    r"|\brank (?:this|me|us|it) (?:first|higher|top)\b"
    r"|\b(?:post|send|forward) (?:this|it|the list|the report) to\b",
    re.IGNORECASE,
)

IRREGULAR_PLURALS = {"person": "people", "child": "children", "man": "men", "woman": "women"}

TIERS = ((4, "Strong"), (2, "Possible"), (0, "Weak"))


class RankError(Exception):
    """Input the script cannot rank; reported in a valid output object, exit 1."""


# --------------------------------------------------------------------------- inputs


def load_inputs(input_file: str | None) -> dict:
    if input_file:
        raw, where = Path(input_file).read_text(encoding="utf-8"), input_file
    elif os.environ.get("INPUT_JSON"):
        raw, where = os.environ["INPUT_JSON"], "INPUT_JSON"
    elif os.environ.get("INPUT_JSON_PATH"):
        path = os.environ["INPUT_JSON_PATH"]
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise RankError(f"INPUT_JSON_PATH points at {path}, which cannot be read: {exc}") from exc
        where = path
    else:
        raise RankError("no inputs: neither INPUT_JSON nor INPUT_JSON_PATH is set")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RankError(f"{where} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RankError(f"{where} must hold one JSON object, got {type(data).__name__}")
    return data


def load_steps(steps_file: str | None, inputs: dict) -> list[tuple[str, Any]] | None:
    """v1 step outputs, in dependency order, as (step id, output) pairs."""
    value: Any = None
    if steps_file:
        try:
            value = json.loads(Path(steps_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RankError(f"--steps {steps_file} cannot be read as JSON: {exc}") from exc
    elif inputs.get("steps") is not None:
        value = inputs["steps"]
    if value is None:
        return None
    if isinstance(value, dict):
        return [(str(k), v) for k, v in value.items()]
    if isinstance(value, list):
        pairs = []
        for i, entry in enumerate(value):
            if isinstance(entry, dict) and "output" in entry and ("id" in entry or "step" in entry):
                pairs.append((str(entry.get("id") or entry.get("step")), entry["output"]))
            else:
                pairs.append((f"steps[{i}]", entry))
        return pairs
    raise RankError("the v1 step outputs must be an object {step id: output} or a list")


def check_inputs(inputs: dict) -> None:
    top_n = inputs.get("top_n")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= 25:
        raise RankError("top_n must be an integer from 1 to 25")
    kind = inputs.get("item_kind")
    if not isinstance(kind, str) or not kind.strip():
        raise RankError("item_kind must be a non-empty string")
    rank_by = inputs.get("rank_by")
    if not isinstance(rank_by, list) or not 1 <= len(rank_by) <= 3:
        raise RankError("rank_by must be a list of 1 to 3 keys")
    for key in rank_by:
        if not isinstance(key, dict) or key.get("field") not in ("verdict", "score", "item_label"):
            raise RankError("each rank_by key needs field verdict, score or item_label")
        if key.get("direction") not in (None, "asc", "desc"):
            raise RankError("rank_by direction must be asc or desc")


# --------------------------------------------------------------------------- helpers


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def cut(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def scrub(value: str) -> str:
    """Remove contact details from a label or reason."""
    return PHONE_RE.sub("[phone]", EMAIL_RE.sub("[email]", value))


def reads_as_instruction(value: str) -> bool:
    return bool(value) and bool(COMMAND_START_RE.search(value) or INJECTION_RE.search(value))


def plural(kind: str, n: int) -> str:
    if n == 1:
        return kind
    lower = kind.lower()
    if lower in IRREGULAR_PLURALS:
        return IRREGULAR_PLURALS[lower]
    if re.search(r"[^aeiou]y$", lower):
        return kind[:-1] + "ies"
    if re.search(r"(s|x|z|ch|sh)$", lower):
        return kind + "es"
    return kind + "s"


def number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if float(value).is_integer() else value


def fmt_number(value: float | int) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def cut_at_sentence(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    head = value[:limit]
    end = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if head.endswith((".", "!", "?")):
        return head
    if end > 0:
        return head[: end + 1]
    return cut(value, limit)


# --------------------------------------------------------------------------- branches


def find_judge(output: Any, depth: int = 0) -> dict | None:
    """The output (or nested step output) that holds an item_result object."""
    if not isinstance(output, dict) or depth > 2:
        return None
    if isinstance(output.get("item_result"), dict):
        return output
    for value in output.values():
        found = find_judge(value, depth + 1)
        if found is not None:
            return found
    return None


def error_text(output: Any) -> str:
    if not isinstance(output, dict) or not output:
        return "empty output"
    for key in ("error", "message", "error_type", "detail"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            inner = value.get("message") or value.get("detail") or value.get("type")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return "no item_result"


def label_from_item(item: Any) -> str:
    """Name a v2 branch from its own item record (never an email or phone)."""
    if not isinstance(item, dict):
        return ""
    record = item.get("item") if isinstance(item.get("item"), dict) else item
    name = text(record.get("name")) or text(record.get("full_name"))
    extra = text(record.get("domain")) or text(record.get("company"))
    if name:
        label = f"{name} ({extra})" if extra else name
    else:
        label = text(item.get("item_key")) if "@" not in text(item.get("item_key")) else ""
    return cut(scrub(label), 120)


class Branch:
    def __init__(self, position: int, name: str) -> None:
        self.position = position
        self.name = name
        self.group = "failed"  # rankable | skipped | failed
        self.judge: dict | None = None
        self.item_result: dict = {}
        self.label = ""
        self.reason = ""
        self.step_errors: dict[str, str] = {}


def classify(judge: dict | None, branch: Branch) -> None:
    if judge is None:
        return
    status = judge["item_result"].get("status")
    branch.judge = judge
    branch.item_result = judge["item_result"]
    if status == "skipped":
        branch.group = "skipped"
    elif status in ("ok", "no_evidence"):
        branch.group = "rankable"
    else:
        branch.group = "failed"
        branch.reason = f"item_result status {status!r} is not ok, no_evidence or skipped"


def v2_branches(entries: list, prepare: dict | None) -> list[Branch]:
    slots = prepare.get("slots") if isinstance(prepare, dict) else None
    by_key = {
        s["item_key"]: text(s.get("label"))
        for s in (slots or [])
        if isinstance(s, dict) and isinstance(s.get("item_key"), str)
    }
    branches = []
    for i, entry in enumerate(entries):
        b = Branch(i, f"branches[{i}]")
        entry = entry if isinstance(entry, dict) else {}
        judge = find_judge(entry.get("outputs"))
        classify(judge, b)
        if isinstance(entry.get("step_errors"), dict):
            b.step_errors = {
                cut(text(k), 80): cut(scrub(text(v)), 200)
                for k, v in entry["step_errors"].items()
                if text(v)
            }
        if judge is None:
            if entry.get("status") == "skipped":
                b.group = "skipped"
            else:
                b.group = "failed"
                b.reason = text(entry.get("error")) or "no item_result"
        if b.group == "failed":
            item = entry.get("item")
            label = ""
            if isinstance(item, dict):
                key = item.get("item_key")
                label = by_key.get(key, "") if isinstance(key, str) else ""
                if not label and slots and isinstance(item.get("index"), int):
                    idx = item["index"]
                    if 0 <= idx < len(slots) and isinstance(slots[idx], dict):
                        label = text(slots[idx].get("label"))
            b.label = cut(scrub(label), 120) if label else label_from_item(item)
        branches.append(b)
    return branches


def v1_branches(steps: list[tuple[str, Any]]) -> tuple[list[Branch], bool]:
    prepare = None
    rest: list[tuple[str, Any]] = []
    for step_id, output in steps:
        if prepare is None and isinstance(output, dict) and isinstance(output.get("slots"), list):
            prepare = output
        else:
            rest.append((step_id, output))
    slots = prepare["slots"] if prepare else []
    branches: list[Branch] = []
    for i, (step_id, output) in enumerate(rest):
        b = Branch(i, step_id)
        classify(find_judge(output), b)
        if b.judge is None:
            b.group = "failed"
            b.reason = error_text(output)
        branches.append(b)

    # Slot of each failed branch: its position, unless a judged branch claimed that key.
    claimed = {text(b.item_result.get("item_key")) for b in branches if b.judge is not None}
    used: set[int] = set()
    for b in branches:
        if b.judge is not None:
            continue
        slot_index = b.position if b.position < len(slots) else None
        slot = slots[slot_index] if slot_index is not None else None
        if isinstance(slot, dict) and text(slot.get("item_key")) in claimed:
            slot_index, slot = None, None
            for j, candidate in enumerate(slots):
                if (
                    isinstance(candidate, dict)
                    and j not in used
                    and text(candidate.get("item_key")) not in claimed
                ):
                    slot_index, slot = j, candidate
                    break
        if prepare is not None and slot_index is not None and slot is None:
            b.group = "skipped"  # nothing was there to judge
            continue
        if slot_index is not None:
            used.add(slot_index)
        b.label = cut(scrub(text(slot.get("label"))), 120) if isinstance(slot, dict) else ""
    return branches, prepare is not None


# --------------------------------------------------------------------------- rubric


def recompute(judge: dict, label: str) -> tuple[dict, list[dict], str | None]:
    """Score, verdict and tier from an ICP-style rubric; (values, corrections, note)."""
    ir = judge["item_result"]
    rubric = judge.get("rubric_used")
    criteria = rubric.get("criteria") if isinstance(rubric, dict) else None
    on_scale = ir.get("score_scale") == "0-5" or "fit_score" in judge
    judge_score = number(ir.get("score"))
    if judge_score is None:
        judge_score = number(judge.get("fit_score"))
    if (
        ir.get("status") != "ok"
        or not isinstance(criteria, list)
        or not criteria
        or not on_scale
        or judge_score is None
    ):
        return {}, [], None

    parsed = []
    for c in criteria:
        if not isinstance(c, dict):
            return {}, [], f"{label}: the judge's rubric has an entry that is not an object; score kept as given."
        kind = c.get("kind", "nice_to_have")
        result = str(c.get("result", "")).strip().lower().replace(" ", "_").replace("-", "_")
        weight = c.get("weight", 1)
        if (
            kind not in ("must_have", "nice_to_have", "exclusion")
            or result not in ("met", "not_met", "unknown")
            or isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or weight <= 0
        ):
            return {}, [], f"{label}: the judge's rubric has a criterion in an unknown shape; score kept as given."
        parsed.append((kind, Decimal(str(weight)), result))

    exclusion_met = any(k == "exclusion" and r == "met" for k, _, r in parsed)
    must_not_met = any(k == "must_have" and r == "not_met" for k, _, r in parsed)
    any_not_met = any(k != "exclusion" and r == "not_met" for k, _, r in parsed)
    total = sum((w for k, w, _ in parsed if k != "exclusion"), Decimal(0))
    met = sum((w for k, w, r in parsed if k != "exclusion" and r == "met"), Decimal(0))
    if exclusion_met:
        score, why = 0, "an exclusion is met"
    else:
        score = round_half_up(Decimal(5) * met / total) if total else 0
        why = f"5 × {fmt_number(float(met))} ÷ {fmt_number(float(total))} met weight"
        if must_not_met and score > 1:
            score, why = 1, "a must-have is not met"
    if score >= 4:
        verdict = "Pursue"
    elif score >= 2 or not (any_not_met or exclusion_met):
        verdict = "Research"
    else:
        verdict = "Reject"
    tier = next(name for floor, name in TIERS if score >= floor)

    corrections = []

    def check(field: str, judge_value: Any, computed: Any) -> None:
        if judge_value is not None and judge_value != computed:
            corrections.append(
                {"item_label": label, "field": field, "judge_value": judge_value, "computed_value": computed}
            )

    check("item_result.score", number(ir.get("score")), score)
    check("fit_score", number(judge.get("fit_score")), score)
    check("item_result.verdict", ir.get("verdict") if isinstance(ir.get("verdict"), str) else None, verdict)
    check("verdict", judge.get("verdict") if isinstance(judge.get("verdict"), str) else None, verdict)
    check("fit_tier", judge.get("fit_tier") if isinstance(judge.get("fit_tier"), str) else None, tier)
    note = None
    if corrections:
        said, now_is = [], []
        for name, fields, computed in (
            ("score", ("item_result.score", "fit_score"), score),
            ("verdict", ("item_result.verdict", "verdict"), verdict),
            ("tier", ("fit_tier",), tier),
        ):
            wrong = [c["judge_value"] for c in corrections if c["field"] in fields]
            if wrong:
                said.append(f"{name} {'/'.join(dict.fromkeys(str(w) for w in wrong))}")
                now_is.append(f"{name} {computed}")
        note = cut(
            f"{label}: the judge gave {', '.join(said)}, which its own rubric does not give "
            f"({why}); ranked with {', '.join(now_is)}.",
            300,
        )
    return {"score": score, "verdict": verdict, "tier": tier}, corrections, note


# --------------------------------------------------------------------------- ordering


def make_comparator(rank_by: list[dict]):
    def compare(a: dict, b: dict) -> int:
        for key in rank_by:
            field = key["field"]
            if field == "verdict":
                order = [o.casefold() for o in key.get("order") or []]
                if not order:
                    continue

                def pos(item: dict) -> int:
                    v = item.get("verdict")
                    return order.index(v.casefold()) if isinstance(v, str) and v.casefold() in order else len(order)

                diff = pos(a) - pos(b)
            elif field == "score":
                sa, sb = a.get("score"), b.get("score")
                if sa is None or sb is None:
                    diff = (sa is None) - (sb is None)
                else:
                    diff = (sb > sa) - (sb < sa) if key.get("direction", "desc") == "desc" else (sa > sb) - (sa < sb)
            else:
                la, lb = a["item_label"].casefold(), b["item_label"].casefold()
                diff = (la > lb) - (la < lb)
                if key.get("direction") == "desc":
                    diff = -diff
            if diff:
                return diff
        la, lb = a["item_label"].casefold(), b["item_label"].casefold()
        if la != lb:
            return -1 if la < lb else 1
        return a["_position"] - b["_position"]

    return functools.cmp_to_key(compare)


def rank_phrase(rank_by: list[dict]) -> str:
    names = []
    for key in rank_by:
        if key["field"] == "verdict" and not key.get("order"):
            continue
        names.append({"verdict": "verdict", "score": "score", "item_label": "name"}[key["field"]])
    return ", then ".join(names) or "name"


# --------------------------------------------------------------------------- sources


class Sources:
    """Renumber branch sources into one list; one id per url (or url-less record_ref)."""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.by_identity: dict[tuple[str, str], int] = {}
        self.maps: dict[int, dict[str, int]] = {}  # branch position -> old id -> entry index

    def add_branch(self, position: int, report: Any) -> None:
        mapping = self.maps.setdefault(position, {})
        sources = report.get("sources") if isinstance(report, dict) else None
        for src in sources if isinstance(sources, list) else []:
            if not isinstance(src, dict):
                continue
            url = src.get("url") if isinstance(src.get("url"), str) and src.get("url") else None
            ref = src.get("record_ref")
            ref = ref if isinstance(ref, str) and RECORD_REF_RE.match(ref) and len(ref) <= 200 else None
            if url is None and ref is None:
                continue
            identity = ("url", url) if url else ("ref", ref)
            index = self.by_identity.get(identity)
            if index is None:
                entry: dict[str, Any] = {}
                if url:
                    entry["url"] = url
                if ref:
                    entry["record_ref"] = ref
                for field in ("title", "published_at", "provider"):
                    if isinstance(src.get(field), str) and src[field]:
                        entry[field] = cut(src[field], 300) if field == "title" else src[field]
                index = len(self.entries)
                self.entries.append(entry)
                self.by_identity[identity] = index
            old = src.get("id")
            if isinstance(old, str) and old not in mapping:
                mapping[old] = index

    def indexes(self, position: int, old_ids: Any) -> list[int]:
        mapping = self.maps.get(position, {})
        out: list[int] = []
        for old in old_ids if isinstance(old_ids, list) else []:
            index = mapping.get(old) if isinstance(old, str) else None
            if index is not None and index not in out:
                out.append(index)
        return out

    def finalize(self, cited: set[int], order_of: list[int]) -> tuple[list[dict], dict[int, str], int]:
        """Keep at most 60 sources (drop uncited ones from the lowest-ranked branch first)."""
        keep = list(range(len(self.entries)))
        dropped = 0
        if len(keep) > MAX_SOURCES:
            removable = [i for i in reversed(order_of) if i not in cited]
            for i in removable:
                if len(keep) <= MAX_SOURCES:
                    break
                keep.remove(i)
                dropped += 1
            while len(keep) > MAX_SOURCES:
                keep.pop()
                dropped += 1
        ids = {index: f"S{n}" for n, index in enumerate(keep, start=1)}
        return [{"id": ids[i], **self.entries[i]} for i in keep], ids, dropped


# --------------------------------------------------------------------------- main build


def build(inputs: dict, steps: list[tuple[str, Any]] | None, now_arg: str | None) -> dict:
    check_inputs(inputs)
    top_n: int = inputs["top_n"]
    kind = inputs["item_kind"].strip()
    rank_by: list[dict] = inputs["rank_by"]
    item_details = inputs.get("item_details") or 0
    if not isinstance(item_details, int) or isinstance(item_details, bool) or not 0 <= item_details <= 10:
        raise RankError("item_details must be an integer from 0 to 10")
    show_score = inputs.get("show_score", True) is not False
    now = text(inputs.get("now")) or text(now_arg)
    unknowns: list[str] = []

    bound = inputs.get("branches")
    cut_count = 0
    prepare = None
    if steps:
        prepare = next((o for _, o in steps if isinstance(o, dict) and isinstance(o.get("slots"), list)), None)
    if bound is not None:
        if isinstance(bound, dict) and isinstance(bound.get("branches"), list):
            entries = bound["branches"]
            if isinstance(bound.get("cut_count"), int):
                cut_count = bound["cut_count"]
        elif isinstance(bound, list):
            entries = bound
        else:
            raise RankError("branches must be a for_each result {branches, cut, cut_count} or its branches list")
        if len(entries) > MAX_RANKED:
            raise RankError(f"branches has {len(entries)} entries; at most {MAX_RANKED} are allowed")
        branches = v2_branches(entries, prepare)
        empty_list = not entries
    elif steps is not None:
        branches, _ = v1_branches(steps)
        empty_list = False
    else:
        raise RankError(
            "no evidence: bind `branches` to the for_each step's output, or pass the v1 step "
            "outputs with --steps <file>"
        )
    if isinstance(inputs.get("cut_count"), int) and not isinstance(inputs.get("cut_count"), bool):
        cut_count = inputs["cut_count"]
    cut_count = max(cut_count, 0)

    for key in rank_by:
        if key["field"] == "verdict" and not key.get("order"):
            unknowns.append("The rank_by key `verdict` has no order, so it was ignored.")

    # Rankable items, with rubric recomputation.
    items: list[dict] = []
    corrections: list[dict] = []
    correction_notes: list[str] = []
    for b in branches:
        if b.group != "rankable":
            continue
        ir = b.item_result
        label = cut(scrub(text(ir.get("item_label"))), 120)
        item: dict[str, Any] = {"_position": b.position, "_branch": b, "item_label": label, "status": ir["status"]}
        key = ir.get("item_key")
        if isinstance(key, str) and "@" not in key and KEY_RE.match(key) and len(key) <= 200:
            item["item_key"] = key
        if isinstance(ir.get("verdict"), str) and ir["verdict"]:
            item["verdict"] = cut(ir["verdict"], 40)
        score = number(ir.get("score"))
        if score is not None:
            item["score"] = score
        if isinstance(ir.get("score_scale"), str) and ir["score_scale"]:
            item["score_scale"] = cut(ir["score_scale"], 20)
        values, fixes, note = recompute(b.judge, label)
        if values:
            item["score"] = values["score"]
            item["verdict"] = values["verdict"]
            item.setdefault("score_scale", "0-5")
        corrections.extend(fixes)
        if note:
            correction_notes.append(note)
        item["headline"] = text(ir.get("headline"))
        items.append(item)

    items.sort(key=make_comparator(rank_by))
    items = items[:MAX_RANKED]

    # Sources: walk ranked items in rank order, each branch's report.sources in order.
    sources = Sources()
    for it in items:
        sources.add_branch(it["_position"], it["_branch"].judge.get("report"))
    order_of = list(range(len(sources.entries)))
    shown = min(top_n, len(items))
    detail_lines: dict[int, list[dict]] = {}
    cited: set[int] = set()
    for rank, it in enumerate(items, start=1):
        it["_src"] = sources.indexes(it["_position"], it["_branch"].item_result.get("source_ids"))
        cited.update(it["_src"])
    source_list, new_ids, dropped = sources.finalize(cited, order_of)
    if dropped:
        unknowns.append(f"Dropped {dropped} sources over the {MAX_SOURCES}-source limit.")

    def ids(indexes: list[int]) -> list[str]:
        return [new_ids[i] for i in indexes if i in new_ids]

    ranked = []
    for rank, it in enumerate(items, start=1):
        entry = {"rank": rank, "item_label": it["item_label"]}
        if "item_key" in it:
            entry["item_key"] = it["item_key"]
        entry["status"] = it["status"]
        for field in ("verdict", "score", "score_scale"):
            if field in it:
                entry[field] = it[field]
        entry["headline"] = cut(it["headline"], 200)
        entry["source_ids"] = ids(it["_src"])
        ranked.append(entry)

    failed_branches = [b for b in branches if b.group == "failed"]
    failed = [
        {"item_label": b.label, "branch": cut(b.name, 80), "reason": cut(scrub(b.reason or "no item_result"), 200)}
        for b in failed_branches[:MAX_FAILED]
    ]
    skipped_count = sum(1 for b in branches if b.group == "skipped")

    # ---- report
    title = text(inputs.get("title")) or f"{kind[:1].upper()}{kind[1:]} ranking"
    n = len(ranked)
    withheld: list[str] = []

    def line(r: dict) -> str:
        head = f"{r['rank']}. {r['item_label']}"
        parts = []
        if "verdict" in r:
            parts.append(r["verdict"])
        if show_score and "score" in r:
            s = fmt_number(r["score"])
            parts.append(f"{s} ({r['score_scale']})" if "score_scale" in r else s)
        if parts:
            head += " — " + ", ".join(parts)
        headline = r["headline"]
        if reads_as_instruction(headline):
            return head + ": headline not shown (it reads as an instruction)."
        if not headline:
            return cut(head, 400)
        return cut(f"{head}: {headline}", 400)

    for r in ranked:
        if reads_as_instruction(r["headline"]):
            withheld.append(
                f"{r['item_label']}: the branch headline reads as an instruction and is not "
                "repeated; it changed no order, score or field."
            )

    def item_label_tag(r: dict) -> str:
        if "verdict" in r:
            return r["verdict"]
        return "no evidence" if r["status"] == "no_evidence" else "no verdict"

    def names_line(prefix: str, labels: list[str]) -> str:
        names = [lbl or "(unnamed)" for lbl in labels]
        full = f"{prefix}: {', '.join(names)}"
        if len(full) <= 400:
            return full
        kept: list[str] = []
        for i, name in enumerate(names):
            rest = len(names) - len(kept) - 1
            candidate = f"{prefix}: {', '.join(kept + [name])}, and {rest} others."
            if len(candidate) > 400:
                break
            kept.append(name)
        others = len(names) - len(kept)
        return cut(f"{prefix}: {', '.join(kept)}, and {others} others." if kept else f"{prefix}.", 400)

    # Detail sections.
    detail_sections: list[dict] = []
    if item_details > 0:
        for r, it in zip(ranked[:shown], items[:shown]):
            report = it["_branch"].judge.get("report")
            if not isinstance(report, dict) or not isinstance(report.get("sections"), list) or not report["sections"]:
                continue
            first = report["sections"][0] if isinstance(report["sections"][0], dict) else {}
            heading = f"{r['item_label']} — {r['verdict']}" if "verdict" in r else r["item_label"]
            section: dict[str, Any] = {"heading": cut(heading, 80)}
            summary = text(report.get("summary"))
            if summary and not reads_as_instruction(summary):
                section["body_md"] = cut_at_sentence(summary, 600)
            elif summary:
                withheld.append(
                    f"{r['item_label']}: the branch summary reads as an instruction and is not "
                    "repeated; it changed no order, score or field."
                )
            lines = []
            for src_item in (first.get("items") or [])[:item_details]:
                if not isinstance(src_item, dict):
                    continue
                t = text(src_item.get("text"))
                if not t:
                    continue
                if reads_as_instruction(t):
                    withheld.append(
                        f"{r['item_label']}: a branch detail line reads as an instruction and is "
                        "not repeated; it changed no order, score or field."
                    )
                    continue
                out: dict[str, Any] = {
                    "text": cut(t, 400),
                    "citations": ids(sources.indexes(it["_position"], src_item.get("citations"))),
                }
                if isinstance(src_item.get("label"), str) and src_item["label"]:
                    out["label"] = cut(src_item["label"], 40)
                else:
                    out["label"] = "detail"
                lines.append(out)
            section["items"] = lines
            detail_sections.append(section)

    sections: list[dict] = []
    extra: list[dict] = []
    if n:
        top_items = [{"text": line(r), "citations": r["source_ids"], "label": item_label_tag(r)} for r in ranked[:shown]]
        more = None
        if n > shown:
            more = {
                "text": names_line(f"…and {n - shown} more", [r["item_label"] for r in ranked[shown:]]),
                "citations": [],
                "label": "more",
            }
            if len(top_items) < MAX_ITEMS:
                top_items.append(more)
            else:
                extra.append({"heading": "Not shown", "items": [more]})
        top = {"heading": cut(f"Top {shown} of {n} {plural(kind, n)}", 80), "items": top_items}
        if not (n == 1 and detail_sections):
            sections.append(top)
    if failed_branches:
        labels = [b.label or b.name for b in failed_branches]
        nj = [
            {"text": cut(f"{b.label or b.name}: {scrub(b.reason or 'no item_result')}", 400), "citations": [], "label": "failed"}
            for b in failed_branches[:NOT_JUDGED_SHOWN]
        ]
        if len(failed_branches) > NOT_JUDGED_SHOWN:
            rest = labels[NOT_JUDGED_SHOWN:]
            nj.append({"text": names_line(f"…and {len(rest)} more not judged", rest), "citations": [], "label": "failed"})
        extra.append({"heading": "Not judged", "items": nj})
    if cut_count:
        extra.append({
            "heading": "Not evaluated",
            "items": [{
                "text": f"…and {cut_count} more {plural(kind, cut_count)} not evaluated (over the limit).",
                "citations": [],
                "label": "not evaluated",
            }],
        })
    room = MAX_SECTIONS - len(sections) - len(extra)
    sections = sections + detail_sections[: max(room, 0)] + extra

    # Summary.
    total = len(branches)
    if empty_list:
        summary = f"Nothing to rank: no {plural(kind, 2)} were evaluated."
    elif n == 0 and not failed_branches:
        summary = f"Nothing to rank: all {skipped_count} slots were empty."
    elif n == 0:
        summary = f"Nothing to rank: {len(failed_branches)} of {total} branches failed."
    elif n == 1 and not failed_branches and not skipped_count and not cut_count:
        only = ranked[0]
        if reads_as_instruction(only["headline"]) or not only["headline"]:
            summary = f"Ranked 1 {kind}: {only['item_label']}" + (f" — {only['verdict']}." if "verdict" in only else ".")
        else:
            summary = f"{only['verdict']}: {only['headline']}" if "verdict" in only else only["headline"]
    else:
        showing = f"the top {shown}" if shown < n else ("it" if n == 1 else f"all {n}")
        summary = (
            f"Ranked {n} {plural(kind, n)} by {rank_phrase(rank_by)}; showing {showing}. "
            f"{len(failed_branches)} failed, {skipped_count} skipped."
        )
    if cut_count:
        summary += f" {cut_count} not evaluated (over the limit)."

    # A branch whose own judgment came back can still have lost a search on the way:
    # name each failed step, so "0 failed" never reads as "nothing went wrong".
    partial = [b for b in branches if b.step_errors and b.group != "failed"]
    step_notes = []
    for b in partial:
        name = b.label or text(b.item_result.get("item_label")) or b.name
        for ref, err in b.step_errors.items():
            step_notes.append(cut(f"{cut(scrub(name), 120)}: step {ref} failed ({err}); judged without it.", 300))
    if partial and n:
        steps = sum(len(b.step_errors) for b in partial)
        summary += f" {steps} step{'s' if steps != 1 else ''} failed inside {len(partial)} {plural(kind, len(partial))}."

    all_unknowns = unknowns + step_notes + correction_notes + withheld
    if len(all_unknowns) > MAX_UNKNOWNS:
        extra_count = len(all_unknowns) - (MAX_UNKNOWNS - 1)
        all_unknowns = all_unknowns[: MAX_UNKNOWNS - 1] + [f"…and {extra_count} more notes (see corrections)."]

    report: dict[str, Any] = {
        "title": cut(title, 150),
        "summary": cut(summary, 600),
        "severity": "attention" if failed_branches else "info",
        "sections": sections,
        "sources": source_list,
        "unknowns": [cut(u, 300) for u in all_unknowns],
    }
    if now:
        report["meta"] = {"skill": SKILL, "skill_version": VERSION, "generated_at": now}

    return {
        "ranked": ranked,
        "shown": shown,
        "skipped_count": skipped_count,
        "failed": failed,
        "corrections": corrections,
        "report": report,
    }


def error_output(detail: str, now: str) -> dict:
    report: dict[str, Any] = {
        "title": "Ranking not built",
        "summary": cut(f"The ranking script could not run: {detail}", 600),
        "severity": "attention",
        "sections": [],
        "sources": [],
        "unknowns": [cut(detail, 300)],
    }
    if now:
        report["meta"] = {"skill": SKILL, "skill_version": VERSION, "generated_at": now}
    return {"ranked": [], "shown": 0, "skipped_count": 0, "failed": [], "corrections": [], "report": report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", help="read the inputs from this JSON file instead of INPUT_JSON")
    parser.add_argument("--steps", help="v1: a JSON file of the prior step outputs {step id: output}")
    parser.add_argument("--now", help="the run's start time, when the `now` input is absent")
    parser.add_argument("--out", default=DEFAULT_OUT, help="also write the output here")
    args = parser.parse_args(argv)

    code = 0
    inputs: dict = {}
    try:
        inputs = load_inputs(args.input)
        result = build(inputs, load_steps(args.steps, inputs), args.now)
        print(
            f"rank: {len(result['ranked'])} ranked, {len(result['failed'])} failed, "
            f"{result['skipped_count']} skipped, {len(result['corrections'])} corrections",
            file=sys.stderr,
        )
    except RankError as exc:
        result, code = error_output(str(exc), text(inputs.get("now")) or text(args.now)), 1
        print(f"rank: error: {exc}", file=sys.stderr)
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    try:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"rank: could not write {args.out}: {exc}", file=sys.stderr)
    print(payload)
    return code


if __name__ == "__main__":
    sys.exit(main())
