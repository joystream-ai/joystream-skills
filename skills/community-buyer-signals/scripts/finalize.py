#!/usr/bin/env python3
"""Build community-buyer-signals' output from the model's scores, so it always matches
assets/output.schema.json.

Run inside JoyStream's sandbox after search.py, once the model has written its scores:

    python3 scripts/finalize.py --scores /tmp/scores.json

The scores file maps each post's source id to its three answers:

    {"S1": {"buying": {"p": 0.8, "quote": "..."}, "pain": {...}, "vendor": {...}}, ...}

Reads the posts and search notes search.py wrote (/tmp/posts.json,
/tmp/search-summary.json) and the step's inputs (INPUT_JSON or INPUT_JSON_PATH, for
product, since_days and now). Copies every post's fields from the posts file, keeps a
quote only when it is copied exactly from that post, picks the buyers, and writes the
report with fixed wording and lengths. Prints one JSON object on stdout (also saved at
/tmp/community-buyer-signals.json). Always exits 0 with a valid object; problems go to
report.unknowns. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SKILL = "community-buyer-signals"
VERSION = "1.1.1"
QUESTIONS = ("buying", "pain", "vendor")
MAX_QUOTE = 300
MAX_ITEMS = 25
MAX_UNKNOWNS = 20
CONTACT_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|\+?\d[\d\s().-]{7,}\d")
PROVIDER = {"hn": "Hacker News", "reddit": "Reddit"}


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_inputs() -> dict:
    raw = os.environ.get("INPUT_JSON")
    if not raw and os.environ.get("INPUT_JSON_PATH"):
        raw = Path(os.environ["INPUT_JSON_PATH"]).read_text(encoding="utf-8")
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def cut(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def probability(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return round(min(1.0, max(0.0, float(value))), 2)


def exact_quote(quote: Any, post: dict) -> tuple[str, bool]:
    """(quote, dropped): the quote when it is copied from the post, cut short before any
    contact detail and at MAX_QUOTE characters; else ("", True)."""
    if not isinstance(quote, str) or not quote.strip():
        return "", False
    quote = quote.strip()
    haystack = " ".join(f"{post.get('title') or ''} {post.get('text') or ''}".split())
    if " ".join(quote.split()) not in haystack:
        return "", True
    contact = CONTACT_RE.search(quote)
    if contact:
        quote = quote[: contact.start()].rstrip()
    return quote[:MAX_QUOTE].rstrip(), False


def answer(raw: Any, post: dict) -> tuple[dict, bool]:
    raw = raw if isinstance(raw, dict) else {}
    p = probability(raw.get("p"))
    quote, dropped = exact_quote(raw.get("quote"), post) if p >= 0.2 else ("", False)
    return {"p": p, "quote": quote}, dropped


def scored_post(post: dict, scores: dict, notes: dict) -> dict:
    raw = scores.get(post["source_id"])
    if not isinstance(raw, dict):
        notes["unscored"].append(post["source_id"])
        raw = {}
    out = {
        "post_id": post["post_id"],
        "source_id": post["source_id"],
        "source": post["source"],
        "kind": post["kind"],
        "url": post["url"],
        "title": str(post.get("title") or "(untitled)")[:300],
        "published_at": post.get("published_at") if isinstance(post.get("published_at"), str) else None,
        "author": str(post.get("author") or "")[:60],
    }
    for question in QUESTIONS:
        out[question], dropped = answer(raw.get(question), post)
        notes["dropped"] += dropped
    return out


def section(heading: str, label: str, posts: list[dict], question: str) -> dict:
    items = []
    for post in posts[:MAX_ITEMS]:
        quote = post[question]["quote"]
        text = f'{post["title"]} - "{quote}"' if quote else post["title"]
        items.append({"text": cut(text, 400), "citations": [post["source_id"]], "label": label})
    return {"heading": heading, "items": items}


def build(inputs: dict, posts: list[dict], scores: dict, search: dict) -> dict:
    notes = {"unscored": [], "dropped": 0}
    scored = [scored_post(p, scores, notes) for p in posts]
    buyers = sorted(
        (p for p in scored if p["buying"]["p"] >= 0.6 and p["vendor"]["p"] < 0.5),
        key=lambda p: -p["buying"]["p"],
    )
    buyer_ids = {p["source_id"] for p in buyers}
    in_pain = [p for p in scored if p["pain"]["p"] >= 0.6 and p["vendor"]["p"] < 0.5]
    pain = [p for p in in_pain if p["source_id"] not in buyer_ids]
    vendors = sum(1 for p in scored if p["vendor"]["p"] >= 0.5)

    since_days = search.get("since_days") or inputs.get("since_days") or 365
    where = " and ".join(sorted({PROVIDER[p["source"]] for p in scored})) or "Hacker News"
    if scored:
        summary = (
            f"Scored {len(scored)} {where} posts from the last {since_days} days: {len(buyers)} look like "
            f"buyers, {len(in_pain)} describe the pain, "
            f"{vendors} are vendors."
        )
    else:
        queries = "; ".join(search.get("queries") or inputs.get("queries") or [])
        summary = cut(f"No Hacker News posts matched {queries} in the last {since_days} days.", 600)

    unknowns = [cut(u, 300) for u in (search.get("unknowns") or []) if isinstance(u, str)]
    if search.get("error"):
        unknowns.insert(0, cut(f"Search failed ({search['error']}): {search.get('detail') or ''}", 300))
        summary = "Hacker News search was not reachable; nothing was scored."
    if notes["unscored"]:
        unknowns.append(cut(f"Not scored by the model: {', '.join(notes['unscored'])}.", 300))
    if notes["dropped"]:
        unknowns.append(f"{notes['dropped']} quote(s) not copied exactly from their post were removed.")

    product = inputs.get("product") if isinstance(inputs.get("product"), str) else ""
    title = cut(f"Community buyer signals for {product}" if product.strip() else "Community buyer signals", 150)
    sections = [s for s in (section("Buyers", "buyer", buyers, "buying"), section("Pain", "pain", pain, "pain")) if s["items"]]
    sources = []
    for p in scored:
        source = {"id": p["source_id"], "url": p["url"], "title": p["title"], "provider": p["source"]}
        if p["published_at"]:
            source["published_at"] = p["published_at"]
        sources.append(source)

    report = {
        "title": title,
        "summary": summary,
        "severity": "attention" if buyers or search.get("error") else "info",
        "sections": sections,
        "sources": sources,
        "unknowns": unknowns[:MAX_UNKNOWNS],
    }
    if isinstance(inputs.get("now"), str) and inputs["now"].strip():
        report["meta"] = {"skill": SKILL, "skill_version": VERSION, "generated_at": inputs["now"].strip()}
    return {"buyers": buyers, "posts": scored, "scored_count": len(scored), "report": report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scores", default="/tmp/scores.json", help="the model's scores, keyed by source id")
    parser.add_argument("--dir", default="/tmp", help="where search.py wrote posts.json and search-summary.json")
    parser.add_argument("--out", default="/tmp/community-buyer-signals.json")
    args = parser.parse_args(argv)
    work = Path(args.dir)
    posts = read_json(work / "posts.json", {}).get("posts") or []
    scores = read_json(Path(args.scores), {})
    if isinstance(scores, dict) and isinstance(scores.get("scores"), dict):
        scores = scores["scores"]
    search = read_json(work / "search-summary.json", {})
    result = build(load_inputs(), [p for p in posts if isinstance(p, dict)], scores if isinstance(scores, dict) else {}, search)
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
