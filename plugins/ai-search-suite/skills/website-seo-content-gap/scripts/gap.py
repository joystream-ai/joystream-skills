#!/usr/bin/env python3
"""Compare a site's page with the pages that rank for each focus keyword, deterministically.

Run inside JoyStream's sandbox by the website-seo-content-gap skill:

    python3 scripts/gap.py

Inputs come from INPUT_JSON or, when that is over 64 KiB, the file named by
INPUT_JSON_PATH. `--input FILE` reads them from a file instead (tests, local runs).

The search results come from earlier search steps (Tavily, Brave, SerpApi, Exa, or a plain
list), because the sandbox holds no credentials. Standard library only. Fetches our page and
the top competing result pages for each keyword with plain GET requests (no JavaScript), then
prints exactly one JSON object on stdout, valid against assets/output.schema.json, and writes
the same object to /tmp/website-seo-content-gap.json. When nothing can be compared it still
prints a valid object and exits 1.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_OUT = "/tmp/website-seo-content-gap.json"
USER_AGENT = "Mozilla/5.0 (compatible; JoyStreamSEOAudit/1.0; +https://joystream.ai)"
TIMEOUT = 15
MAX_BYTES = 3_000_000
PAUSE = 0.2
MAX_FETCHES = 45
MAX_KEYWORDS = 5
MAX_TOPICS = 8
SKIP_EXT = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|pdf|zip|gz|mp4|mp3|webm|css|js|json|xml|txt|docx?|xlsx?|pptx?)$", re.IGNORECASE)
SKIP_HOSTS = ("youtube.com", "youtu.be", "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com", "linkedin.com", "reddit.com", "quora.com", "pinterest.com")
SCHEMA_TYPES = ["FAQPage", "HowTo", "Article", "BlogPosting", "Product", "SoftwareApplication", "AggregateRating", "VideoObject", "BreadcrumbList"]
SOURCE_NAMES = {"tavily": "Tavily", "brave": "Brave Search", "serpapi": "SerpApi (Google)", "exa": "Exa", "list": "the given result list"}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
STOPWORDS = frozenset(
    """
    the and for with from that this these those your you our are was were will can how what why when where who which
    into onto about over under than then them they their there here has have had not but all any each more most less
    best top new get use using used does did just also its it's out off one two three per via vs versus way ways
    guide introduction overview conclusion summary table contents step steps tips list examples example key why
    frequently asked questions question faq faqs answer answers read learn more related posts article blog home
    """.split()
)


# ---------------------------------------------------------------- fetching


def fetch(url: str) -> dict[str, Any]:
    """GET one URL, following redirects. Never raises: a failure is status 0 and an error."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(MAX_BYTES)
            status, final, headers = response.status, response.geturl(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return {"url": url, "final_url": url, "status": exc.code, "headers": {}, "text": "", "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"url": url, "final_url": url, "status": 0, "headers": {}, "text": "", "error": type(exc).__name__}
    headers = {k.lower(): v for k, v in headers.items()}
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", headers.get("content-type", ""))
    if match:
        charset = match.group(1)
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    return {"url": url, "final_url": final, "status": status, "headers": headers, "text": text, "error": None}


class Fetcher:
    """Caches pages by URL and stops at MAX_FETCHES requests per run."""

    def __init__(self) -> None:
        self.cache: dict[str, dict[str, Any]] = {}
        self.count = 0
        self.exhausted = False

    def page(self, url: str) -> dict[str, Any]:
        key = normalize(url)
        if key in self.cache:
            return self.cache[key]
        if self.count >= MAX_FETCHES:
            self.exhausted = True
            return {"error": "fetch limit reached"}
        if self.count:
            time.sleep(PAUSE)
        self.count += 1
        result = fetch(url)
        if result["error"]:
            summary: dict[str, Any] = {"error": result["error"]}
        elif "html" not in result["headers"].get("content-type", "text/html").lower():
            summary = {"error": "not HTML"}
        else:
            summary = summarize(result["text"])
            summary["final_url"] = result["final_url"]
        self.cache[key] = summary
        return summary


def normalize(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def host_key(url: str) -> str:
    """The host without www., with a non-default port kept (so two local servers differ)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port and port not in (80, 443) else host


def skipped_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in SKIP_HOSTS)


def short(url: str) -> str:
    return urlsplit(url).path or "/"


# ---------------------------------------------------------------- parsing


class PageParser(HTMLParser):
    """Collects the title, H1-H3 headings, JSON-LD and main text of one HTML document."""

    SKIP = ("head", "script", "style", "noscript", "nav", "footer", "header", "svg", "template")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self._in_title = False
        self.headings: dict[str, list[str]] = {"h1": [], "h2": [], "h3": []}
        self._heading: str | None = None
        self._heading_text: list[str] = []
        self.json_ld: list[str] = []
        self._in_json_ld = False
        self._json_ld_text: list[str] = []
        self._skip_depth = 0
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title" and self.title is None:
            self._in_title, self.title = True, ""
        elif tag in self.headings:
            self._heading, self._heading_text = tag, []
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_json_ld, self._json_ld_text = True, []
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in self.headings and self._heading == tag:
            text = re.sub(r"\s+", " ", "".join(self._heading_text)).strip()
            if text:
                self.headings[tag].append(text)
            self._heading = None
        elif tag == "script" and self._in_json_ld:
            self.json_ld.append("".join(self._json_ld_text))
            self._in_json_ld = False
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title and self.title is not None:
            self.title += data
        if self._heading:
            self._heading_text.append(data)
        if self._in_json_ld:
            self._json_ld_text.append(data)
        elif not self._skip_depth:
            self.text.append(data)


def json_ld_types(blocks: list[str]) -> list[str]:
    types: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            kind = node.get("@type")
            for value in kind if isinstance(kind, list) else [kind]:
                if isinstance(value, str) and value not in types:
                    types.append(value)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    for block in blocks:
        try:
            walk(json.loads(block))
        except ValueError:
            pass
    return types


def summarize(html: str) -> dict[str, Any]:
    parser = PageParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML still yields what was parsed
        pass
    title = re.sub(r"\s+", " ", parser.title or "").strip() or None
    body = re.sub(r"\s+", " ", " ".join(parser.text)).strip()
    sub = parser.headings["h2"] + parser.headings["h3"]
    types = json_ld_types(parser.json_ld)
    full = " ".join([title or ""] + parser.headings["h1"] + sub + [body])
    return {
        "title": title,
        "words": len(re.findall(r"\w+", body)),
        "subheadings": sub,
        "schema_types": types,
        "has_faq": "FAQPage" in types or sum(1 for h in sub if h.rstrip().endswith("?")) >= 3,
        "tokens": " " + " ".join(re.findall(r"[a-z0-9]+", full.lower())) + " ",
        "error": None,
    }


# ---------------------------------------------------------------- search results


def unwrap(item: Any) -> tuple[bool, Any, str | None, str | None]:
    """(ok, payload, explicit keyword, error) for one serps item."""
    keyword = None
    if isinstance(item, dict) and "results" in item and isinstance(item.get("keyword"), str):
        keyword, item = item["keyword"], item["results"]
    for _ in range(4):
        if isinstance(item, dict) and isinstance(item.get("step"), dict):
            item = item["step"]
        if isinstance(item, dict) and "ok" in item and ("data" in item or item.get("ok") is False):
            if item.get("ok") is False:
                return False, None, keyword, str(item.get("error") or "the search step failed")
            item = item.get("data")
            continue
        break
    return True, item, keyword, None


def parse_results(payload: Any) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    """(source, query, [{position, url, title}]) from any supported search payload."""
    rows: list[Any]
    source: str | None
    query: str | None = None
    if isinstance(payload, list):
        source, rows = "list", payload
    elif isinstance(payload, dict) and isinstance(payload.get("organic_results"), list):
        source, rows = "serpapi", payload["organic_results"]
        params = payload.get("search_parameters")
        query = params.get("q") if isinstance(params, dict) else None
    elif isinstance(payload, dict) and isinstance(payload.get("web"), dict) and isinstance(payload["web"].get("results"), list):
        source, rows = "brave", payload["web"]["results"]
        q = payload.get("query")
        query = q.get("original") if isinstance(q, dict) else None
    elif isinstance(payload, dict) and isinstance(payload.get("results"), list):
        if isinstance(payload.get("query"), str):
            source, query = "tavily", payload["query"]
        else:
            source = "exa"
        rows = payload["results"]
    else:
        return None, None, []
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        url = row.get("url") or row.get("link")
        if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
            continue
        position = row.get("position") if source == "serpapi" and isinstance(row.get("position"), int) else index
        title = row.get("title") if isinstance(row.get("title"), str) else None
        results.append({"position": position, "url": url, "title": title})
    return source, (query.strip() if isinstance(query, str) and query.strip() else None), results


# ---------------------------------------------------------------- comparing


def words_of(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def heading_terms(headings: list[str], drop: set[str]) -> dict[str, str]:
    """{term: first heading it came from}: unigrams and adjacent-word bigrams of the
    headings, leaving out short words, stopwords, numbers and the keyword's own words."""
    terms: dict[str, str] = {}

    def keep(word: str) -> bool:
        return len(word) >= 3 and not word.isdigit() and word not in STOPWORDS and word not in drop

    for heading in headings:
        words = words_of(heading)
        for i, word in enumerate(words):
            if keep(word):
                terms.setdefault(word, heading)
                if i + 1 < len(words) and keep(words[i + 1]):
                    terms.setdefault(f"{word} {words[i + 1]}", heading)
    return terms


def missing_topics(competitors: list[dict[str, Any]], ours: dict[str, Any], drop: set[str]) -> list[dict[str, Any]]:
    readable = [c for c in competitors if c["error"] is None]
    if len(readable) < 2:
        return []
    threshold = max(2, math.ceil(len(readable) / 2))
    counts: dict[str, int] = {}
    examples: dict[str, list[str]] = {}
    for comp in readable:
        for term, heading in heading_terms(comp["subheadings"], drop).items():
            counts[term] = counts.get(term, 0) + 1
            if len(examples.setdefault(term, [])) < 2:
                examples[term].append(heading)
    missing = [t for t, n in counts.items() if n >= threshold and f" {t} " not in ours["tokens"]]
    rank = lambda t: (-counts[t], t)  # noqa: E731
    bigrams = sorted((t for t in missing if " " in t), key=rank)
    unigrams = sorted((t for t in missing if " " not in t), key=rank)
    kept = set(bigrams)
    while True:
        pool = [b for b in bigrams if b in kept] + [u for u in unigrams if not any(u in b.split() for b in kept)]
        top = sorted(pool, key=rank)[:MAX_TOPICS]
        now_kept = {t for t in top if " " in t}
        if now_kept == kept:
            break
        kept = now_kept
    return [{"term": t, "competitors": counts[t], "example_headings": examples[t]} for t in top]


def choose_our_page(keyword: str, pages: list[Any], host: str, origin: str) -> str:
    wanted = {w for w in words_of(keyword) if len(w) >= 3 and w not in STOPWORDS}
    best, best_score = None, 0
    for page in pages:
        if not isinstance(page, dict):
            continue
        url = page.get("final_url") or page.get("url")
        if not isinstance(url, str) or host_key(url) != host:
            continue
        if isinstance(page.get("status"), int) and page["status"] != 200:
            continue
        parts = [page.get("title") or ""] + list(page.get("h1") or []) + list(page.get("h2") or [])
        have = set(words_of(" ".join(p for p in parts if isinstance(p, str))))
        score = len(wanted & have)
        if score > best_score:
            best, best_score = url, score
    return best or origin + "/"


def compare(keyword: str, source: str, results: list[dict[str, Any]], host: str, origin: str, pages: list[Any], max_competitors: int, fetcher: Fetcher, unknowns: list[str]) -> dict[str, Any]:
    our_rank = next((r["position"] for r in results if host_key(r["url"]) == host), None)
    our_page = next((r["url"] for r in results if host_key(r["url"]) == host), None) or choose_our_page(keyword, pages, host, origin)
    competitors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in results:
        if len(competitors) >= max_competitors:
            break
        url = row["url"]
        if host_key(url) == host or skipped_host(url) or SKIP_EXT.search(urlsplit(url).path) or normalize(url) in seen:
            continue
        seen.add(normalize(url))
        page = fetcher.page(url)
        if page["error"]:
            unknowns.append(f'{url} could not be read ({page["error"]}), so it is left out of the comparison for "{keyword}".')
            competitors.append({"url": url, "position": row["position"], "title": row["title"], "words": None, "headings": None, "schema_types": [], "has_faq": False, "error": page["error"], "subheadings": []})
        else:
            competitors.append({"url": url, "position": row["position"], "title": page["title"] or row["title"], "words": page["words"], "headings": len(page["subheadings"]), "schema_types": page["schema_types"], "has_faq": page["has_faq"], "error": None, "subheadings": page["subheadings"]})
    ours = fetcher.page(our_page)
    readable = [c for c in competitors if c["error"] is None]
    drop = set(words_of(keyword))
    topics: list[dict[str, Any]] = []
    schema_gap: list[str] = []
    if ours["error"]:
        unknowns.append(f'Our page {our_page} could not be read ({ours["error"]}), so "{keyword}" was not compared.')
    elif len(readable) < 2:
        unknowns.append(f'Fewer than 2 top results for "{keyword}" could be read, so no subtopic gap was computed.')
    else:
        topics = missing_topics(competitors, ours, drop)
        for kind in SCHEMA_TYPES:
            if kind not in ours["schema_types"] and sum(1 for c in readable if kind in c["schema_types"]) >= 2:
                schema_gap.append(kind)
    median = int(round(statistics.median(c["words"] for c in readable))) if readable else None
    for comp in competitors:
        del comp["subheadings"]
    return {
        "keyword": keyword,
        "source": source,
        "our_rank": our_rank,
        "our_page": our_page,
        "our_words": None if ours["error"] else ours["words"],
        "competitor_median_words": median,
        "competitors": competitors,
        "missing_topics": topics,
        "missing_schema": schema_gap,
        "_results": len(results),
        "_readable": len(readable),
    }


def finding(fid: str, severity: str, title: str, pages: list[str], evidence: str, fix: str, effort: str) -> dict[str, Any]:
    return {"id": fid, "category": "content_gap", "severity": severity, "title": title, "pages": pages, "evidence": evidence, "fix": fix, "effort": effort}


def findings_for(entry: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    k, page = entry["keyword"], entry["our_page"]
    key = k.lower()
    pages = [page] if page else []
    topics, n = entry["missing_topics"], entry["_readable"]
    if topics:
        rank = entry["our_rank"]
        severity = ("high" if rank is None or rank > 5 else "medium") if len(topics) >= 3 else "low"
        listed = ", ".join(f'"{t["term"]}" ({t["competitors"]} of {n})' for t in topics[:5])
        names = ", ".join(t["term"] for t in topics[:4])
        found.append(finding(f"content-gap:{key}", severity, f'{short(page)} misses subtopics the top results for "{k}" cover', pages, f"Headings in the top results: {listed}; none of these appear on {short(page)}.", f"Add sections to {short(page)} on {names}, answered with your own facts, where they fit what the page offers.", "project"))
    ours, median = entry["our_words"], entry["competitor_median_words"]
    if ours is not None and median and ours < 0.5 * median:
        found.append(finding(f"content-depth:{key}", "medium", f'{short(page)} is much shorter than the top results for "{k}"', pages, f"{short(page)} has {ours} words; the readable top results have a median of {median}.", "Expand the page with what a searcher for this phrase needs: specifics, examples, proof and answers to common questions.", "project"))
    if entry["missing_schema"]:
        types = ", ".join(entry["missing_schema"])
        found.append(finding(f"schema-gap:{key}", "low", f'Top results for "{k}" use structured data {short(page)} lacks', pages, f"{types} JSON-LD on 2 or more top results; none on {short(page)}.", f"Add {types} JSON-LD only where the page really has that content (FAQPage only for a visible FAQ).", "quick fix"))
    if entry["our_rank"] is None and entry["_results"]:
        name = SOURCE_NAMES[entry["source"]]
        caveat = "" if entry["source"] == "serpapi" else f" (this is {name}'s result order, not Google's rank)"
        found.append(finding(f"not-ranking:{key}", "medium", f'The site is not in the top results for "{k}"', pages, (f"The only result from {name} is not on this site{caveat}." if entry["_results"] == 1 else f"None of the {entry['_results']} results from {name} is on this site{caveat}."), f'Make {short(page)} the page for "{k}": the phrase in its title and H1, and the subtopics the top results cover.', "project"))
    return found


# ---------------------------------------------------------------- output


def empty_counts() -> dict[str, int]:
    return {level: 0 for level in SEVERITY_ORDER}


def error_output(site: str, keywords: list[dict[str, Any]], unknowns: list[str]) -> dict[str, Any]:
    return {"outcome": "error", "site": site, "keywords": keywords, "findings": [], "counts": empty_counts(), "target_urls": [], "sentence": "", "unknowns": unknowns}


def blank_entry(keyword: str, source: str | None) -> dict[str, Any]:
    return {"keyword": keyword, "source": source, "our_rank": None, "our_page": None, "our_words": None, "competitor_median_words": None, "competitors": [], "missing_topics": [], "missing_schema": []}


def keyword_list(raw: Any) -> list[str]:
    items = re.split(r"[\n,]+", raw) if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return [k.strip() for k in items if isinstance(k, str) and k.strip()]


def build(inputs: dict[str, Any]) -> tuple[dict[str, Any], int]:
    raw = str(inputs.get("site_url") or "").strip()
    if not raw:
        return error_output("", [], ["site_url is missing."]), 1
    if "//" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    if not parts.hostname:
        return error_output(raw, [], ["site_url is not a URL."]), 1
    origin = f"{parts.scheme or 'https'}://{parts.netloc}"
    host = host_key(origin)
    serps = inputs.get("serps")
    if isinstance(serps, dict):
        serps = [serps]
    if not isinstance(serps, list) or not serps:
        return error_output(origin, [], ["No search results were given (serps), so nothing was compared."]), 1
    focus = keyword_list(inputs.get("focus_keywords"))
    max_competitors = inputs.get("max_competitors") if isinstance(inputs.get("max_competitors"), int) else 5
    max_competitors = max(1, min(max_competitors, 8))
    pages = inputs.get("pages") if isinstance(inputs.get("pages"), list) else []

    unknowns: list[str] = []
    if len(serps) > MAX_KEYWORDS:
        unknowns.append(f"Only the first {MAX_KEYWORDS} of {len(serps)} searches were compared.")
    fetcher = Fetcher()
    entries: list[dict[str, Any]] = []
    failed = 0
    sources: dict[str, list[str]] = {}
    for index, item in enumerate(serps[:MAX_KEYWORDS]):
        ok, payload, explicit, error = unwrap(item)
        source, query, results = parse_results(payload) if ok else (None, None, [])
        keyword = explicit or query or (focus[index] if index < len(focus) else f"search {index + 1}")
        if not ok or not results:
            failed += 1
            reason = error if not ok else "no results"
            unknowns.append(f'The search for "{keyword}" returned {reason}, so it was not compared.')
            entries.append(blank_entry(keyword, source))
            continue
        sources.setdefault(source, []).append(keyword)
        entries.append(compare(keyword, source, results, host, origin, pages, max_competitors, fetcher, unknowns))

    if failed == len(entries):
        return error_output(origin, entries, unknowns), 1

    for source, words in sources.items():
        if source != "serpapi":
            quoted = ", ".join(f'"{w}"' for w in words)
            unknowns.append(f"Positions for {quoted} come from {SOURCE_NAMES[source]}'s result order, not a live Google ranking.")
    if fetcher.exhausted:
        unknowns.append(f"The {MAX_FETCHES}-page fetch limit was reached; later results were not read.")
    unknowns.append("Pages were read as raw HTML without running JavaScript; a result that renders its content in the browser looks thinner than it is.")

    found = [f for entry in entries if "_results" in entry for f in findings_for(entry)]
    found.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], -len(f["pages"]), f["id"]))
    counts = {level: sum(1 for f in found if f["severity"] == level) for level in SEVERITY_ORDER}
    partial = failed > 0 or any(c["error"] for e in entries for c in e["competitors"]) or any("_results" in e and e["our_words"] is None for e in entries)
    compared = [e for e in entries if "_results" in e]
    for entry in compared:
        del entry["_results"], entry["_readable"]

    gapped = [e for e in compared if e["missing_topics"]]
    if gapped:
        terms = list(dict.fromkeys(t["term"] for e in gapped for t in e["missing_topics"]))[:3]
        sentence = f"Content gaps on {len(gapped)} of {len(entries)} keywords: top results cover {', '.join(terms)} that our pages do not."
    elif found:
        tally = ", ".join(f"{counts[level]} {level}" for level in SEVERITY_ORDER if counts[level])
        sentence = f"No subtopic gaps on {len(entries)} keywords; other findings: {tally} (depth, structured data or ranking)."
    else:
        sentence = f"No content gaps against the top results for {len(entries)} keyword(s)."
    output = {
        "outcome": "partial" if partial else "ok",
        "site": origin,
        "keywords": entries,
        "findings": found,
        "counts": counts,
        "target_urls": list(dict.fromkeys(url for f in found for url in f["pages"]))[:15],
        "sentence": sentence,
        "unknowns": unknowns,
    }
    return output, 0


def read_inputs(path: str | None) -> dict[str, Any]:
    if path:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    file_path = os.environ.get("INPUT_JSON_PATH")
    if file_path:
        with open(file_path, encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(os.environ.get("INPUT_JSON") or "{}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input")
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()
    try:
        inputs = read_inputs(args.input)
        output, code = build(inputs if isinstance(inputs, dict) else {})
    except (OSError, ValueError) as exc:
        print(f"could not read the inputs: {type(exc).__name__}", file=sys.stderr)
        output, code = error_output("", [], ["The inputs could not be read."]), 1
    text = json.dumps(output, ensure_ascii=False)
    try:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        print(f"could not write {args.out}: {type(exc).__name__}", file=sys.stderr)
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
