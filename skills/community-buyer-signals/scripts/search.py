#!/usr/bin/env python3
"""Collect public Hacker News (and optionally Reddit) posts for community-buyer-signals.

Run inside JoyStream's sandbox by the community-buyer-signals skill, in one command:

    python3 scripts/search.py

Inputs come from the step's environment: INPUT_JSON (all inputs as one JSON object) or,
when that is over 64 KiB, the file named by INPUT_JSON_PATH. `--input FILE` reads them
from a file instead (tests, local runs).

Sources, both free and keyless (this script never needs, reads or sends a credential,
and never posts anything):

- Hacker News search by relevance: https://hn.algolia.com/api/v1/search
- Reddit search JSON (only with include_reddit): https://www.reddit.com/search.json
  Cloud IPs are often refused; any non-200 answer is a skipped source, never an error.

Writes every kept post to /tmp/posts.json, the same posts split into pages of at most
~40 KB (/tmp/posts-1.json, ...) so each page can be read whole, what finalize.py needs
to /tmp/search-summary.json, and prints one compact
JSON summary on stdout. Exits 1 (with a JSON summary holding `error`) only when the
inputs are unusable or every Hacker News request failed. Standard library only.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HN_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_ITEM = "https://news.ycombinator.com/item?id="
REDDIT_SEARCH = "https://www.reddit.com/search.json"
REDDIT_BASE = "https://www.reddit.com"
USER_AGENT = "joystream-community-buyer-signals/1.0 (public-search collector; +https://joystream.ai)"

TAGS = ("story", "comment", "ask_hn", "show_hn")
MAX_QUERIES = 5
MAX_POSTS = 60
MAX_TEXT = 6000
EXCERPT = 600
PAGE_BYTES = 40_000
TIMEOUT_S = 20

TAG_RE = re.compile(r"<[^>]+>")
BREAK_RE = re.compile(r"<\s*(?:p|br|/p|li|/li|pre|/pre)\b[^>]*>", re.IGNORECASE)


class InputError(Exception):
    pass


# --------------------------------------------------------------------------- inputs


def load_inputs(input_file: str | None) -> dict:
    if input_file:
        raw = Path(input_file).read_text(encoding="utf-8")
    elif os.environ.get("INPUT_JSON"):
        raw = os.environ["INPUT_JSON"]
    elif os.environ.get("INPUT_JSON_PATH"):
        try:
            raw = Path(os.environ["INPUT_JSON_PATH"]).read_text(encoding="utf-8")
        except OSError as exc:
            raise InputError(f"INPUT_JSON_PATH cannot be read: {exc}") from exc
    else:
        raise InputError("no inputs: neither INPUT_JSON nor INPUT_JSON_PATH is set")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(f"inputs are not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError("inputs must be one JSON object")
    return data


def parse_now(value: Any) -> datetime:
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise InputError(f"now {value!r} is not an ISO 8601 time") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def settings(inputs: dict) -> dict:
    queries = inputs.get("queries")
    if isinstance(queries, str):
        queries = [queries]
    if not isinstance(queries, list):
        raise InputError("queries must be a list of 1 to 5 strings")
    queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    queries = list(dict.fromkeys(queries))
    if not 1 <= len(queries) <= MAX_QUERIES:
        raise InputError("queries must hold 1 to 5 non-empty strings")
    tags = inputs.get("tags") or ["story", "comment"]
    if not isinstance(tags, list) or not tags or any(t not in TAGS for t in tags):
        raise InputError(f"tags must be a non-empty list of {', '.join(TAGS)}")
    since_days = inputs.get("since_days", 365)
    if isinstance(since_days, bool) or not isinstance(since_days, int) or not 1 <= since_days <= 3650:
        raise InputError("since_days must be an integer from 1 to 3650")
    max_posts = inputs.get("max_posts", 30)
    if isinstance(max_posts, bool) or not isinstance(max_posts, int) or not 1 <= max_posts <= MAX_POSTS:
        raise InputError(f"max_posts must be an integer from 1 to {MAX_POSTS}")
    must_include = inputs.get("must_include") or ""
    if not isinstance(must_include, str) or len(must_include) > 100:
        raise InputError("must_include must be a string of at most 100 characters")
    now = parse_now(inputs.get("now"))
    return {
        "queries": queries,
        "must_include": [w.lower() for w in must_include.split()],
        "tags": list(dict.fromkeys(tags)),
        "since_days": since_days,
        "max_posts": max_posts,
        "include_reddit": inputs.get("include_reddit") is True,
        "now": now,
        "cutoff": int(now.timestamp()) - since_days * 86400,
    }


# --------------------------------------------------------------------------- http


def fetch_json(url: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    """GET a JSON document; (status, parsed body or None). Never raises for HTTP errors."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 - fixed https hosts
            status, body = response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, str(exc)
    try:
        return status, json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None


# --------------------------------------------------------------------------- shaping


def clean(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    value = BREAK_RE.sub("\n", value)
    value = html.unescape(TAG_RE.sub("", value))
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n\n", value)
    return value.strip()


def cap(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def iso(epoch: Any) -> str | None:
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hn_kind(tags: list) -> str:
    for kind in ("ask_hn", "show_hn", "comment", "story"):
        if kind in tags:
            return kind
    return "story"


def hn_post(hit: dict, query: str) -> dict | None:
    object_id = str(hit.get("objectID") or "").strip()
    if not object_id.isdigit():
        return None
    tags = hit.get("_tags") if isinstance(hit.get("_tags"), list) else []
    kind = hn_kind(tags)
    if kind == "comment":
        title = clean(hit.get("story_title")) or "(comment)"
        body = clean(hit.get("comment_text"))
    else:
        title = clean(hit.get("title")) or "(untitled)"
        body = clean(hit.get("story_text"))
    text = cap(body or title, MAX_TEXT)
    post = {
        "post_id": f"hn:{object_id}",
        "source": "hn",
        "kind": kind,
        "url": HN_ITEM + object_id,
        "title": cap(title, 300),
        "author": str(hit.get("author") or "")[:60],
        "published_at": iso(hit.get("created_at_i")) or hit.get("created_at"),
        "points": hit.get("points") if isinstance(hit.get("points"), int) else None,
        "num_comments": hit.get("num_comments") if isinstance(hit.get("num_comments"), int) else None,
        "query": query,
        "text": text,
        "excerpt": cap(text, EXCERPT),
    }
    story_id = hit.get("story_id")
    if kind == "comment" and isinstance(story_id, int):
        post["thread_url"] = f"{HN_ITEM}{story_id}"
    link = hit.get("url") if kind != "comment" else None
    if isinstance(link, str) and link.startswith(("http://", "https://")):
        post["link_url"] = link
    return post


def reddit_post(child: dict, query: str) -> dict | None:
    data = child.get("data") if isinstance(child, dict) else None
    if not isinstance(data, dict):
        return None
    permalink = data.get("permalink")
    if not isinstance(permalink, str) or not permalink.startswith("/"):
        return None
    title = clean(data.get("title")) or "(untitled)"
    text = cap(clean(data.get("selftext")) or title, MAX_TEXT)
    return {
        "post_id": f"reddit:{data.get('id') or permalink}",
        "source": "reddit",
        "kind": "story",
        "url": REDDIT_BASE + permalink,
        "title": cap(title, 300),
        "author": str(data.get("author") or "")[:60],
        "subreddit": str(data.get("subreddit") or "")[:60],
        "published_at": iso(data.get("created_utc")),
        "points": data.get("score") if isinstance(data.get("score"), int) else None,
        "num_comments": data.get("num_comments") if isinstance(data.get("num_comments"), int) else None,
        "query": query,
        "text": text,
        "excerpt": cap(text, EXCERPT),
        "_created": data.get("created_utc"),
    }


# --------------------------------------------------------------------------- search


def search_hn(query: str, cfg: dict, unknowns: list[str]) -> tuple[list[dict], bool]:
    """(posts, ok). When the strict query finds nothing, retries once with its words
    optional, except the must_include words (a company name), which stay required."""
    base = {
        "query": query,
        "tags": "(" + ",".join(cfg["tags"]) + ")",
        "numericFilters": f"created_at_i>{cfg['cutoff']}",
        "hitsPerPage": str(cfg["max_posts"]),
    }
    anchor = cfg["must_include"]
    if anchor and not all(w in query.lower().split() for w in anchor):
        base["query"] = query = " ".join(anchor) + " " + query
    optional = [w for w in query.split() if w.lower() not in anchor]
    for attempt, extra in enumerate(({}, {"optionalWords": " ".join(optional)})):
        url = HN_SEARCH + "?" + urllib.parse.urlencode({**base, **extra})
        status, body = fetch_json(url)
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("hits"), list):
            why = body if isinstance(body, str) else f"HTTP {status}"
            unknowns.append(f"Hacker News search for {query!r} failed ({why}).")
            return [], False
        hits = body["hits"]
        if hits or attempt == 1 or len(optional) < (1 if anchor else 2):
            if attempt == 1 and hits:
                kept = f" (still requiring {' '.join(anchor)!r})" if anchor else ""
                unknowns.append(f"Hacker News found nothing with every word of {query!r}; used the closest matches{kept} instead.")
            return [p for p in (hn_post(h, query) for h in hits if isinstance(h, dict)) if p], True
    return [], True


def search_reddit(query: str, cfg: dict, unknowns: list[str]) -> list[dict]:
    days = cfg["since_days"]
    window = "day" if days <= 1 else "week" if days <= 7 else "month" if days <= 31 else "year" if days <= 365 else "all"
    url = REDDIT_SEARCH + "?" + urllib.parse.urlencode(
        {"q": query, "sort": "relevance", "t": window, "limit": str(min(cfg["max_posts"], 100)), "raw_json": "1"}
    )
    status, body = fetch_json(url)
    children = body.get("data", {}).get("children") if isinstance(body, dict) else None
    if status != 200 or not isinstance(children, list):
        why = body if isinstance(body, str) else f"HTTP {status}"
        unknowns.append(f"Reddit search for {query!r} skipped ({why}); Reddit often refuses cloud IPs.")
        return []
    posts = []
    for child in children:
        post = reddit_post(child, query)
        if post is None:
            continue
        created = post.pop("_created")
        if isinstance(created, (int, float)) and created <= cfg["cutoff"]:
            continue
        posts.append(post)
    return posts


def interleave(lists: list[list[dict]], limit: int) -> list[dict]:
    """Round-robin by rank across queries, one post per id, at most *limit*."""
    seen: set[str] = set()
    out: list[dict] = []
    depth = max((len(lst) for lst in lists), default=0)
    for i in range(depth):
        for lst in lists:
            if i < len(lst) and lst[i]["post_id"] not in seen:
                seen.add(lst[i]["post_id"])
                out.append(lst[i])
                if len(out) >= limit:
                    return out
    return out


def write_pages(posts: list[dict], out_dir: Path) -> list[str]:
    pages: list[list[dict]] = [[]]
    size = 0
    for post in posts:
        n = len(json.dumps(post, ensure_ascii=False).encode("utf-8"))
        if pages[-1] and size + n > PAGE_BYTES:
            pages.append([])
            size = 0
        pages[-1].append(post)
        size += n
    paths = []
    for i, page in enumerate(pages, start=1):
        path = out_dir / f"posts-{i}.json"
        path.write_text(json.dumps({"page": i, "pages": len(pages), "posts": page}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        paths.append(str(path))
    return paths


def collect(inputs: dict, out_dir: Path) -> tuple[int, dict]:
    cfg = settings(inputs)
    unknowns: list[str] = []
    hn_lists, hn_ok = [], 0
    for query in cfg["queries"]:
        posts, ok = search_hn(query, cfg, unknowns)
        hn_ok += ok
        hn_lists.append(posts)
        time.sleep(0.2)  # be polite to a free API
    reddit_lists = []
    if cfg["include_reddit"]:
        for query in cfg["queries"]:
            reddit_lists.append(search_reddit(query, cfg, unknowns))
    if not hn_ok:
        return 1, {"error": "hn_unavailable", "detail": "every Hacker News request failed", "unknowns": unknowns}

    posts = interleave(hn_lists + reddit_lists, cfg["max_posts"])
    for n, post in enumerate(posts, start=1):
        post["source_id"] = f"S{n}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "posts.json").write_text(json.dumps({"posts": posts}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    pages = write_pages(posts, out_dir)
    found = sum(len(lst) for lst in hn_lists + reddit_lists)
    if not posts:
        unknowns.append("No posts matched any query in the window.")
    (out_dir / "search-summary.json").write_text(
        json.dumps({"queries": cfg["queries"], "since_days": cfg["since_days"], "unknowns": unknowns}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0, {
        "posts_file": str(out_dir / "posts.json"),
        "pages": pages,
        "queries": cfg["queries"],
        "tags": cfg["tags"],
        "since": iso(cfg["cutoff"]),
        "found": found,
        "kept": len(posts),
        "by_source": {
            "hn": sum(1 for p in posts if p["source"] == "hn"),
            "reddit": sum(1 for p in posts if p["source"] == "reddit"),
        },
        "unknowns": unknowns,
        "preview": [
            {"source_id": p["source_id"], "kind": p["kind"], "title": cap(p["title"], 90), "url": p["url"]}
            for p in posts[:10]
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", help="read the inputs from this JSON file instead of INPUT_JSON")
    parser.add_argument("--out-dir", default="/tmp", help="where posts.json and its pages go")
    args = parser.parse_args(argv)
    try:
        code, summary = collect(load_inputs(args.input), Path(args.out_dir))
    except InputError as exc:
        code, summary = 1, {"error": "invalid_input", "detail": str(exc)}
    if code:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "search-summary.json").write_text(json.dumps(summary, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"search: {summary.get('kept', 0)} posts kept", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
