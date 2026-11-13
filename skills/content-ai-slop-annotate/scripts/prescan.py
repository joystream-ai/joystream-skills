#!/usr/bin/env python3
"""Deterministic pre-scan for the content-ai-slop-annotate taxonomy.

Counts the mechanical tells (lists of three, formulaic transitions, false contrasts,
em dashes, fixed phrases, paragraph openers, sentence cadence, short-form scaffolding)
and applies the pattern thresholds, so whether a pattern is flagged is decided the same
way on every run. Judgement calls are left to whoever writes the fixes: categories 6 and
11 entirely, and every instance marked needs_judgment.

Standard library only. Reads the draft, prints one JSON object on stdout, writes nothing
and opens no network connection.

    python3 scripts/prescan.py --file draft.md
    INPUT_TEXT="..." python3 scripts/prescan.py

Exit codes: 0 with the JSON on stdout; 2 when the draft is empty.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

VERSION = "1.1.1"

NAMES = {
    1: "false contrast",
    2: "rule-of-three",
    3: "formulaic transition",
    4: "empty intensifier",
    5: "meta-commentary",
    6: "manufactured balance",
    7: "opener repetition",
    8: "uniform cadence",
    9: "pseudo-precision",
    10: "over-structured short-form",
    11: "summary ending",
    12: "nominalization bloat",
    13: "em-dash overuse",
}
# Pattern categories need this many instances in the piece before any of them is
# flagged; every other category flags a single instance (SKILL.md, "Thresholds").
THRESHOLDS = {1: 2, 2: 3, 3: 2, 13: 3}
# #7's threshold is the length of one run of matching openers, applied while finding runs,
# so each run found is one flaggable instance.
OPENER_RUN_MIN = 3
# Only a run that repeats a narrative subject ("We tried X. We tried Y.") can be a refrain
# building to a point; "When X / When Y / When Z" or "This means / This creates" is the tell.
REFRAIN_OPENERS = {"i", "we", "you", "he", "she", "they", "our", "my"}
MODEL_JUDGED = (6, 11)
CADENCE_MIN_SENTENCES = 4
CADENCE_MAX_SPREAD = 3
SHORT_FORM_MAX_WORDS = 200
SHORT_FORM_MIN_STRUCTURE_LINES = 2

APOS = "['’]"


def _ci(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern.replace("'", APOS), re.IGNORECASE)


TRANSITION_OPENERS = _ci(
    r"^[\s\"“*_]*(moreover|furthermore|additionally|that said|with that said|overall|"
    r"in summary|in conclusion|at the end of the day)\b,?"
)
TRANSITION_ANYWHERE = _ci(
    r"\b(?:it's|it is) (?:worth noting|important to (?:remember|note)) that\b"
)
# (pattern, needs_judgment): judgement where the word has an ordinary literal sense.
INTENSIFIERS = [
    (r"leverag(?:e|es|ed|ing)", False),
    (r"unlock(?:s|ed|ing)?", False),
    (r"harness(?:es|ed|ing)?", True),
    (r"elevat(?:e|es|ed|ing)", False),
    (r"foster(?:s|ed|ing)?", True),
    (r"navigat(?:e|es|ed|ing)", True),
    (r"delv(?:e|es|ed|ing) into", False),
    (r"(?:div(?:e|es|ing)|dove) into", True),
    (r"unpack(?:s|ed|ing)?", True),
    (r"underscor(?:e|es|ed|ing)", False),
    (r"testament to", False),
    (r"bustling", False),
    (r"vibrant", False),
    (r"robust", False),
    (r"seamless(?:ly)?", False),
    (r"game[- ]chang(?:ing|er|ers)", False),
    (r"revolutionar(?:y|ies)|revolutioniz(?:e|es|ed|ing)", False),
    (r"cutting[- ]edge", False),
    (r"paradigm shifts?", False),
    (r"landscapes?", True),
    (r"realms?", True),
    (r"tapestr(?:y|ies)", False),
    (r"journeys?", True),
]
META = _ci(
    r"\b(?:here's the thing|here is the thing|let that sink in|let's break (?:this|it) down|"
    r"let us break (?:this|it) down|the bottom line is|so what does (?:this|that) mean\??)"
)
NOMINALIZATIONS = _ci(
    r"\b(?:due to the fact that|in order to|(?:made|make|makes|making) (?:a|the) decision to|"
    r"(?:has|have|had|having) the ability to)\b"
)
PSEUDO_PRECISION = _ci(
    r"\b\d+(?:\.\d+)?\s?%|\b\d+(?:\.\d+)? percent\b|\bcountless\b|"
    r"\b(?:nearly|almost|virtually) every\b|"
    r"\b(?:studies|research) (?:show|shows|suggest|suggests|prove|proves)\b|\bexperts agree\b"
)
_NEG = r"(?:isn't|is not|wasn't|was not|aren't|are not|weren't|were not)"
FALSE_CONTRASTS = [
    # "It's not just X, it's Y" / "isn't just about X. It's about Y"
    _ci(
        r"\b(?:it's|it is|this is|that's|that is|isn't|is not|wasn't|was not)\s+(?:not\s+)?"
        r"(?:just|only|merely|simply)\b[^.!?]*?[.!?,;—]\s*(?:it's|it is|it was|but|rather)\b"
    ),
    # "X wasn't the problem. Y was." / "The problem was never X. It was always Y."
    _ci(
        rf"\b(?:{_NEG}|was never|is never)\b[^.!?]*[.!?]\s+"
        r"(?:it|that|this|they)(?:'s|'re| is| was| are| were)\b"
    ),
    # "This isn't about X — it's about Y"
    _ci(rf"\b{_NEG}\b[^.!?]*?(?:—|--|;)\s*(?:it's|it is|it was)\b"),
]
EM_DASH = re.compile(r"—| -- ")

# A first item that is really an introductory phrase ("Of course, X, and Y").
INTRO_PHRASES = {
    "of course", "however", "moreover", "furthermore", "that said", "in conclusion",
    "in summary", "overall", "yes", "no", "so", "well", "still", "also", "then", "first",
    "second", "finally", "for example", "in short", "sure", "today", "now", "instead",
}
# A segment that opens like this is a clause, not a list item.
NOT_ITEM_START = re.compile(
    r"^(?:and|or|but|so|because|which|while|when|if|as|since|although|though|not|who|"
    r"where|that|yet)\b",
    re.IGNORECASE,
)
AND_OR_START = re.compile(r"^(?:and|or)\s", re.IGNORECASE)
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")
SENTENCE_END = re.compile(r"(?<=[.!?])[\"”)]?\s+(?=[\"“(*_]?[A-Z0-9])")
BOLD_HEADER_LINE = re.compile(r"^\s*\*\*[^*\n]+\*\*")
LIST_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
OPENER_WORD = re.compile(r"^[\s*_>#\-•\d.)\"“]*([A-Za-z][A-Za-z'’]*)")


def read_text(argv: list[str]) -> str:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", help="read the draft from this file")
    args = parser.parse_args(argv)
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if os.environ.get("INPUT_TEXT"):
        return os.environ["INPUT_TEXT"]
    raw = os.environ.get("INPUT_JSON")
    if raw:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            return data["text"]
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def paragraphs_of(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


def sentences_of(paragraph: str, *, prose_only: bool = False) -> list[str]:
    """Sentences, one list item or header line at most per sentence. ``prose_only`` drops
    list and bold-header lines, which have no cadence to judge."""
    out: list[str] = []
    for line in paragraph.splitlines():
        line = line.strip()
        if not line:
            continue
        is_structure = bool(LIST_LINE.match(line) or BOLD_HEADER_LINE.match(line))
        if prose_only and is_structure:
            continue
        line = LIST_LINE.sub("", line)
        out.extend(s.strip() for s in SENTENCE_END.split(line) if s.strip())
    return out


def word_count(s: str) -> int:
    return len(WORD.findall(s))


def instance(paragraph: int, text: str, *, needs_judgment: bool = False, **extra) -> dict:
    return {"paragraph": paragraph, "text": text, "needs_judgment": needs_judgment, **extra}


def regex_instances(paras: list[str], pattern: re.Pattern[str], **kw) -> list[dict]:
    return [instance(i, m.group(0), **kw) for i, p in enumerate(paras, 1) for m in pattern.finditer(p)]


def find_false_contrasts(paras: list[str]) -> list[dict]:
    found = []
    for i, p in enumerate(paras, 1):
        spans: list[tuple[int, int]] = []
        for pattern in FALSE_CONTRASTS:
            for m in pattern.finditer(p):
                if not any(m.start() < e and s < m.end() for s, e in spans):
                    spans.append((m.start(), m.end()))
        found += [instance(i, p[s:e]) for s, e in sorted(spans)]
    return found


def triplets_in(sentence: str) -> list[list[str]]:
    s = re.sub(r"\s*\([^)]*\)", "", sentence).strip().rstrip(".!?")
    segs = [x.strip() for x in re.split(r",\s+", s) if x.strip()]
    found: list[list[str]] = []
    seen: set[str] = set()

    def add(items: list[str]) -> None:
        key = items[-1].lower()
        if key not in seen:
            seen.add(key)
            found.append(items)

    def intro(i: int, first: str) -> bool:
        return i == 0 and first.lower() in INTRO_PHRASES

    # "A, B, and C"
    for i in range(len(segs) - 2):
        a, b, c = segs[i : i + 3]
        if AND_OR_START.match(c) and not NOT_ITEM_START.match(b) and not intro(i, a):
            add([a, b, c])
    # "A, B and C" with a short middle item
    for i in range(len(segs) - 1):
        a, b = segs[i], segs[i + 1]
        if NOT_ITEM_START.match(b) or intro(i, a):
            continue
        if i + 2 < len(segs) and AND_OR_START.match(segs[i + 2]):
            continue
        m = re.match(r"^(\S+(?:\s+\S+){0,3})\s+(and|or)\s+(\S.*)$", b, re.IGNORECASE)
        if m:
            add([a, m.group(1), m.group(3)])
    # "...: A, B, C" with no conjunction
    if ":" in s:
        tail = [x.strip() for x in re.split(r",\s+", s.split(":", 1)[1]) if x.strip()]
        if len(tail) == 3 and not any(AND_OR_START.match(t) for t in tail):
            add(tail)
    return found


def find_triplets(paras: list[str]) -> list[dict]:
    return [
        instance(i, sentence, items=items)
        for i, p in enumerate(paras, 1)
        for sentence in sentences_of(p)
        for items in triplets_in(sentence)
    ]


def find_transitions(paras: list[str]) -> list[dict]:
    found = []
    for i, p in enumerate(paras, 1):
        for sentence in sentences_of(p):
            m = TRANSITION_OPENERS.match(sentence)
            if m:
                found.append(instance(i, m.group(0).strip(" \"“*_")))
        found += [instance(i, m.group(0)) for m in TRANSITION_ANYWHERE.finditer(p)]
    return found


def find_intensifiers(paras: list[str]) -> list[dict]:
    found = []
    for pattern, judgement in INTENSIFIERS:
        found += regex_instances(paras, re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE),
                                 needs_judgment=judgement)
    return sorted(found, key=lambda x: x["paragraph"])


def find_opener_runs(paras: list[str]) -> list[dict]:
    words = []
    for p in paras:
        m = OPENER_WORD.match(p)
        words.append(m.group(1).lower().replace("’", "'") if m else "")
    found, start = [], 0
    for i in range(1, len(words) + 1):
        if i == len(words) or words[i] != words[start] or not words[start]:
            if words[start] and i - start >= OPENER_RUN_MIN:
                found.append(instance(
                    start + 1,
                    f'paragraphs {start + 1}-{i} all open with "{paras[start].split()[0]}"',
                    needs_judgment=words[start] in REFRAIN_OPENERS,
                    paragraphs=list(range(start + 1, i + 1)),
                    **({"note": "a narrative refrain building to a stated turn is not a tell"}
                       if words[start] in REFRAIN_OPENERS else {}),
                ))
            start = i
    return found


def find_flat_cadence(paras: list[str]) -> list[dict]:
    found = []
    for i, p in enumerate(paras, 1):
        counts = [word_count(s) for s in sentences_of(p, prose_only=True)]
        if len(counts) >= CADENCE_MIN_SENTENCES and max(counts) - min(counts) <= CADENCE_MAX_SPREAD:
            found.append(instance(
                i,
                f"paragraph {i}: {len(counts)} sentences of {min(counts)}-{max(counts)} words",
                sentence_word_counts=counts,
            ))
    return found


def find_short_form_scaffolding(text: str) -> list[dict]:
    lines = text.splitlines()
    headers = sum(1 for line in lines if BOLD_HEADER_LINE.match(line))
    lists = sum(1 for line in lines if LIST_LINE.match(line))
    words = word_count(text)
    if words < SHORT_FORM_MAX_WORDS and headers + lists >= SHORT_FORM_MIN_STRUCTURE_LINES:
        return [instance(
            1,
            f"{words}-word piece with {headers} bold mini-header line(s) and {lists} list line(s)",
            needs_judgment=True,
            note="structure that does real organizational work is not a tell",
        )]
    return []


def decide(category: int, instances: list[dict]) -> dict:
    threshold = THRESHOLDS.get(category, 1)
    count = len(instances)
    decision = "none" if count == 0 else "flag" if count >= threshold else "borderline"
    return {
        "id": category,
        "name": NAMES[category],
        "threshold": threshold,
        "count": count,
        "decision": decision,
        "instances": instances,
    }


def prescan(text: str) -> dict:
    paras = paragraphs_of(text)
    found = {
        1: find_false_contrasts(paras),
        2: find_triplets(paras),
        3: find_transitions(paras),
        4: find_intensifiers(paras),
        5: regex_instances(paras, META),
        7: find_opener_runs(paras),
        8: find_flat_cadence(paras),
        9: regex_instances(paras, PSEUDO_PRECISION, needs_judgment=True),
        10: find_short_form_scaffolding(text),
        12: regex_instances(paras, NOMINALIZATIONS),
        13: regex_instances(paras, EM_DASH),
    }
    for item in found[13]:
        item["text"] = item["text"].strip()
    counts = {
        "triplets": len(found[2]),
        "transitions": len(found[3]),
        "false_contrasts": len(found[1]),
        "em_dashes": len(found[13]),
    }
    return {
        "skill": "content-ai-slop-annotate",
        "version": VERSION,
        "stats": {
            "words": word_count(text),
            "paragraphs": len(paras),
            "sentences": sum(len(sentences_of(p)) for p in paras),
        },
        "counts": counts,
        "counts_line": (
            f"triplets {counts['triplets']} · transitions {counts['transitions']} · "
            f"false contrasts {counts['false_contrasts']} · em dashes {counts['em_dashes']}"
        ),
        "categories": [decide(c, found[c]) for c in sorted(found)],
        "model_judged": [{"id": c, "name": NAMES[c]} for c in MODEL_JUDGED],
    }


def main(argv: list[str]) -> int:
    text = read_text(argv)
    if not text.strip():
        print(json.dumps({"error": "empty_input", "detail": "no draft text was given"}), file=sys.stderr)
        return 2
    print(json.dumps(prescan(text), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
