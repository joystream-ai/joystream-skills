#!/usr/bin/env python3
"""Audit a website's on-page SEO from its raw HTML, deterministically.

Run inside JoyStream's sandbox by the website-seo-page-audit skill:

    python3 scripts/audit.py

Inputs come from INPUT_JSON or, when that is over 64 KiB, the file named by
INPUT_JSON_PATH. `--input FILE` reads them from a file instead (tests, local runs).

Standard library only. Fetches robots.txt, the sitemap and up to `max_pages` pages of the
site with plain GET requests (no JavaScript), then prints exactly one JSON object on stdout,
valid against assets/output.schema.json, and writes the same object to
/tmp/website-seo-page-audit.json. On bad input it still prints a valid object and exits 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

DEFAULT_OUT = "/tmp/website-seo-page-audit.json"
USER_AGENT = "Mozilla/5.0 (compatible; JoyStreamSEOAudit/1.0; +https://joystream.ai)"
TIMEOUT = 15
MAX_BYTES = 3_000_000
PAUSE = 0.2
SKIP_EXT = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|pdf|zip|gz|mp4|mp3|webm|css|js|json|xml|txt|woff2?)$", re.IGNORECASE)
KEY_PATHS = re.compile(r"(pricing|price|product|platform|feature|solution|about|company|faq|docs|blog|customer|case|use-case|integration)", re.IGNORECASE)
LOW_VALUE = re.compile(r"(terms|privacy|cookie|legal|imprint|gdpr|login|log-in|signin|sign-in|signup|sign-up|careers?|jobs)", re.IGNORECASE)
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SEVERITY_POINTS = {"critical": 4.0, "high": 2.0, "medium": 1.0, "low": 0.5}
CATEGORIES = [
    ("indexability", "Indexability"),
    ("titles_metas", "Titles & meta descriptions"),
    ("headings_content", "Headings & content"),
    ("structured_social", "Structured data & social"),
    ("internal_links", "Internal links & images"),
    ("technical", "Technical"),
]


# ---------------------------------------------------------------- fetching


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None


def fetch(url: str, follow: bool = True) -> dict[str, Any]:
    """GET one URL. Never raises: a failure is a result with status 0 and an error."""
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            body = response.read(MAX_BYTES)
            status, final, headers = response.status, response.geturl(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_BYTES) if exc.fp else b""
        status, final, headers = exc.code, url, dict(exc.headers.items()) if exc.headers else {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"url": url, "final_url": url, "status": 0, "headers": {}, "text": "", "ms": int((time.monotonic() - started) * 1000), "error": type(exc).__name__}
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", headers.get("Content-Type", "") or headers.get("content-type", ""))
    if match:
        charset = match.group(1)
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    return {"url": url, "final_url": final, "status": status, "headers": {k.lower(): v for k, v in headers.items()}, "text": text, "ms": int((time.monotonic() - started) * 1000), "error": None}


def normalize(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def same_site(url: str, host: str) -> bool:
    other = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return other == host


# ---------------------------------------------------------------- parsing


class PageParser(HTMLParser):
    """Collects what an SEO audit reads from one HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self._in_title = False
        self.metas: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []  # (rel, href)
        self.anchors: list[str] = []
        self.h1: list[str] = []
        self.h2: list[str] = []
        self._heading: str | None = None
        self._heading_text: list[str] = []
        self.json_ld: list[str] = []
        self._in_json_ld = False
        self._json_ld_text: list[str] = []
        self.lang: str | None = None
        self.images = 0
        self.images_missing_alt = 0
        self._skip_depth = 0
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.lang = a["lang"]
        elif tag == "title" and self.title is None:
            self._in_title = True
            self.title = ""
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or a.get("http-equiv") or "").lower()
            if key and key not in self.metas:
                self.metas[key] = a.get("content", "").strip()
        elif tag == "link":
            self.links.append((a.get("rel", "").lower(), a.get("href", "").strip()))
            if "alternate" in a.get("rel", "").lower() and a.get("hreflang"):
                self.metas.setdefault("hreflang_count", "0")
                self.metas["hreflang_count"] = str(int(self.metas["hreflang_count"]) + 1)
        elif tag == "a" and a.get("href"):
            self.anchors.append(a["href"].strip())
        elif tag in ("h1", "h2"):
            self._heading, self._heading_text = tag, []
        elif tag == "img":
            self.images += 1
            if not a.get("alt", "").strip() and a.get("role") != "presentation" and a.get("aria-hidden") != "true":
                self.images_missing_alt += 1
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_json_ld, self._json_ld_text = True, []
        if tag in ("head", "script", "style", "noscript", "nav", "footer", "header", "svg", "template"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in ("h1", "h2") and self._heading == tag:
            text = re.sub(r"\s+", " ", "".join(self._heading_text)).strip()
            if text:
                (self.h1 if tag == "h1" else self.h2).append(text)
            self._heading = None
        elif tag == "script" and self._in_json_ld:
            self.json_ld.append("".join(self._json_ld_text))
            self._in_json_ld = False
        if tag in ("head", "script", "style", "noscript", "nav", "footer", "header", "svg", "template") and self._skip_depth:
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


def json_ld_types(blocks: list[str]) -> tuple[list[str], int]:
    """(@type values found, number of blocks that are not valid JSON)."""
    types: list[str] = []
    invalid = 0

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
            invalid += 1
    return types, invalid


def robots_rules(text: str) -> tuple[list[str], list[str], list[str]]:
    """(sitemaps, Disallow rules for Googlebot or *, Allow rules for the same group)."""
    sitemaps, groups, current, agents_open = [], {}, [], False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        field = field.lower()
        if field == "sitemap" and value:
            sitemaps.append(value)
        elif field == "user-agent":
            if not agents_open:
                current = []
            current.append(value.lower())
            groups.setdefault(value.lower(), {"disallow": [], "allow": []})
            agents_open = True
        elif field in ("disallow", "allow"):
            agents_open = False
            for agent in current:
                groups.setdefault(agent, {"disallow": [], "allow": []})[field].append(value)
    group = groups.get("googlebot") or groups.get("*") or {"disallow": [], "allow": []}
    return sitemaps, [d for d in group["disallow"] if d], group["allow"]


def blocked(path: str, disallow: list[str], allow: list[str]) -> bool:
    def match(rule: str) -> int:
        pattern = re.escape(rule).replace(r"\*", ".*")
        if pattern.endswith(r"\$"):
            pattern = pattern[:-2] + "$"
        return len(rule) if re.match(pattern, path) else -1

    best_disallow = max((match(r) for r in disallow), default=-1)
    best_allow = max((match(r) for r in allow), default=-1)
    return best_disallow >= 0 and best_disallow > best_allow


def sitemap_urls(start: list[str], host: str) -> tuple[list[str], dict[str, Any]]:
    seen: list[str] = []
    info = {"url": None, "status": None, "url_count": 0}
    queue = list(dict.fromkeys(start))
    fetched = 0
    while queue and fetched < 4:
        url = queue.pop(0)
        result = fetch(url)
        fetched += 1
        if info["url"] is None:
            info["url"], info["status"] = url, result["status"]
        if result["status"] != 200:
            continue
        if info["status"] != 200:
            info["url"], info["status"] = url, 200
        try:
            root = ElementTree.fromstring(result["text"].encode("utf-8"))
        except ElementTree.ParseError:
            continue
        for loc in root.iter():
            if not loc.tag.endswith("loc") or not loc.text:
                continue
            value = loc.text.strip()
            if root.tag.endswith("sitemapindex"):
                queue.append(value)
            elif same_site(value, host) and value not in seen:
                seen.append(value)
    info["url_count"] = len(seen)
    return seen, info


# ---------------------------------------------------------------- auditing


def audit_page(result: dict[str, Any], host: str, sitemap: set[str], disallow: list[str], allow: list[str]) -> dict[str, Any]:
    parser = PageParser()
    is_html = "html" in result["headers"].get("content-type", "html").lower()
    if result["status"] == 200 and is_html:
        try:
            parser.feed(result["text"])
        except Exception:  # noqa: BLE001 - malformed HTML still yields what was parsed
            pass
    final = result["final_url"]
    canonical = next((urljoin(final, href) for rel, href in parser.links if "canonical" in rel.split() and href), None)
    if canonical is None:
        canonical_state = "missing"
    elif normalize(canonical) == normalize(final):
        canonical_state = "self"
    else:
        canonical_state = "other"
    robots_meta = ", ".join(v for k, v in parser.metas.items() if k in ("robots", "googlebot") and v)
    x_robots = result["headers"].get("x-robots-tag", "")
    text = re.sub(r"\s+", " ", " ".join(parser.text)).strip()
    words = len(re.findall(r"\w+", text))
    internal, external = set(), set()
    for href in parser.anchors:
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(final, href)
        if urlsplit(absolute).scheme not in ("http", "https"):
            continue
        (internal if same_site(absolute, host) else external).add(normalize(absolute))
    types, invalid = json_ld_types(parser.json_ld)
    title = re.sub(r"\s+", " ", parser.title or "").strip() or None
    description = parser.metas.get("description") or None
    path = urlsplit(final).path or "/"
    return {
        "url": result["url"],
        "final_url": final,
        "status": result["status"],
        "error": result["error"],
        "response_ms": result["ms"],
        "redirected": normalize(final) != normalize(result["url"]),
        "title": title,
        "title_length": len(title) if title else 0,
        "meta_description": description,
        "meta_description_length": len(description) if description else 0,
        "canonical": canonical,
        "canonical_state": canonical_state,
        "noindex": "noindex" in (robots_meta + " " + x_robots).lower(),
        "robots_meta": (robots_meta or x_robots) or None,
        "h1": parser.h1[:3],
        "h1_count": len(parser.h1),
        "h2": parser.h2[:8],
        "word_count": words,
        "json_ld_types": types,
        "json_ld_invalid": invalid,
        "open_graph": {k: bool(parser.metas.get(f"og:{k}")) for k in ("title", "description", "image")},
        "lang": parser.lang,
        "images": parser.images,
        "images_missing_alt": parser.images_missing_alt,
        "internal_links_out": sorted(internal)[:200],
        "external_links_out": len(external),
        "inbound_internal_links": 0,
        "in_sitemap": normalize(final) in sitemap or normalize(result["url"]) in sitemap,
        "blocked_by_robots": blocked(path, disallow, allow),
        "excerpt": text[:500],
    }


def short(url: str) -> str:
    return urlsplit(url).path or "/"


def finding(fid: str, category: str, severity: str, title: str, pages: list[str], evidence: str, fix: str, effort: str) -> dict[str, Any]:
    return {"id": fid, "category": category, "severity": severity, "title": title, "pages": pages, "evidence": evidence, "fix": fix, "effort": effort}


def findings_for(pages: list[dict[str, Any]], home: dict[str, Any] | None, site: dict[str, Any], keywords: list[str]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    ok = [p for p in pages if p["status"] == 200]
    home_url = home["final_url"] if home else None

    def where(items: list[dict[str, Any]]) -> list[str]:
        return [p["final_url"] for p in items]

    # Indexability
    if site["robots_blocks_all"]:
        found.append(finding("robots-blocks-site", "indexability", "critical", "robots.txt blocks Googlebot from the whole site", [], 'robots.txt has "Disallow: /" for Googlebot or *.', "Remove the site-wide Disallow, or narrow it to the paths that should stay private.", "quick fix"))
    noindex = [p for p in ok if p["noindex"]]
    if noindex:
        sev = "critical" if any(p["final_url"] == home_url or p["in_sitemap"] for p in noindex) else "high"
        found.append(finding("noindex", "indexability", sev, "Pages tell search engines not to index them", where(noindex), "; ".join(f'{short(p["final_url"])}: "{p["robots_meta"]}"' for p in noindex[:5]), "Remove noindex from pages that should rank, or drop them from the sitemap if it is intended.", "quick fix"))
    broken = [p for p in pages if p["status"] >= 400 or p["status"] == 0]
    if broken:
        found.append(finding("broken-pages", "indexability", "high", "Pages return an error", where(broken), "; ".join(f'{short(p["url"])}: {p["status"] or p["error"]}' for p in broken[:5]), "Fix or redirect these URLs, and remove dead ones from the sitemap.", "quick fix"))
    robots_blocked = [p for p in ok if p["blocked_by_robots"] and p["in_sitemap"]]
    if robots_blocked:
        found.append(finding("sitemap-blocked", "indexability", "high", "Sitemap lists pages that robots.txt blocks", where(robots_blocked), ", ".join(short(p["final_url"]) for p in robots_blocked[:5]), "Unblock them in robots.txt or remove them from the sitemap.", "quick fix"))
    canon_other = [p for p in ok if p["canonical_state"] == "other"]
    if canon_other:
        sev = "high" if any(p["final_url"] == home_url for p in canon_other) else "medium"
        found.append(finding("canonical-elsewhere", "indexability", sev, "Canonical tag points to a different URL", where(canon_other), "; ".join(f'{short(p["final_url"])} → {p["canonical"]}' for p in canon_other[:5]), "Point each page's canonical at itself unless it really is a duplicate.", "quick fix"))
    canon_missing = [p for p in ok if p["canonical_state"] == "missing"]
    if canon_missing:
        found.append(finding("canonical-missing", "indexability", "low", "Pages have no canonical tag", where(canon_missing), f"{len(canon_missing)} of {len(ok)} pages.", "Add a self-referencing canonical link to each page template.", "quick fix"))
    if site["sitemap_status"] != 200:
        found.append(finding("no-sitemap", "indexability", "medium", "No readable XML sitemap", [], f'{site["sitemap_url"] or "/sitemap.xml"} returned {site["sitemap_status"]}.', "Publish /sitemap.xml with every page that should rank and declare it in robots.txt.", "quick fix"))
    elif not site["sitemap_declared"]:
        found.append(finding("sitemap-undeclared", "indexability", "low", "robots.txt does not declare the sitemap", [], "No Sitemap: line in robots.txt.", f'Add "Sitemap: {site["sitemap_url"]}" to robots.txt.', "quick fix"))

    # Titles and meta descriptions
    no_title = [p for p in ok if not p["title"]]
    if no_title:
        found.append(finding("title-missing", "titles_metas", "high", "Pages have no title", where(no_title), ", ".join(short(p["final_url"]) for p in no_title[:5]), "Give every page a unique title of 30-60 characters that leads with what the page is about.", "quick fix"))
    long_titles = [p for p in ok if p["title_length"] > 60]
    if long_titles:
        found.append(finding("title-long", "titles_metas", "medium", "Titles are cut off in search results (over 60 characters)", where(long_titles), "; ".join(f'{short(p["final_url"])}: {p["title_length"]} chars' for p in long_titles[:5]), "Shorten to 60 characters or fewer, keeping the main topic first.", "quick fix"))
    short_titles = [p for p in ok if 0 < p["title_length"] < 20]
    if short_titles:
        found.append(finding("title-short", "titles_metas", "medium", "Titles are too short to say what the page is about", where(short_titles), "; ".join(f'{short(p["final_url"])}: "{p["title"]}"' for p in short_titles[:5]), "Expand to 30-60 characters with the page's topic and the brand.", "quick fix"))
    titles: dict[str, list[dict[str, Any]]] = {}
    for p in ok:
        if p["title"]:
            titles.setdefault(p["title"].lower(), []).append(p)
    dupes = [group for group in titles.values() if len(group) > 1]
    if dupes:
        pages_dup = [p for group in dupes for p in group]
        found.append(finding("title-duplicate", "titles_metas", "high", "Several pages share the same title", where(pages_dup), "; ".join(f'"{g[0]["title"]}" on {len(g)} pages' for g in dupes[:3]), "Write a distinct title for each page.", "quick fix"))
    no_desc = [p for p in ok if not p["meta_description"]]
    if no_desc:
        sev = "high" if home and home in no_desc else "medium"
        found.append(finding("description-missing", "titles_metas", sev, "Pages have no meta description", where(no_desc), f"{len(no_desc)} of {len(ok)} pages: " + ", ".join(short(p["final_url"]) for p in no_desc[:5]), "Write a 120-155 character description per page that answers what it offers and who it is for.", "quick fix"))
    long_desc = [p for p in ok if p["meta_description_length"] > 160]
    if long_desc:
        found.append(finding("description-long", "titles_metas", "low", "Meta descriptions are cut off (over 160 characters)", where(long_desc), "; ".join(f'{short(p["final_url"])}: {p["meta_description_length"]} chars' for p in long_desc[:5]), "Trim to 155 characters or fewer.", "quick fix"))
    descs: dict[str, list[dict[str, Any]]] = {}
    for p in ok:
        if p["meta_description"]:
            descs.setdefault(p["meta_description"].lower(), []).append(p)
    ddupes = [group for group in descs.values() if len(group) > 1]
    if ddupes:
        found.append(finding("description-duplicate", "titles_metas", "medium", "Several pages share one meta description", where([p for g in ddupes for p in g]), f"{len(ddupes)} description(s) reused across pages.", "Write a distinct description for each page.", "quick fix"))
    for keyword in keywords:
        k = keyword.lower()
        if not any(k in (p["title"] or "").lower() or any(k in h.lower() for h in p["h1"]) for p in ok):
            found.append(finding(f"keyword-untargeted:{k}", "titles_metas", "high", f'No page targets "{keyword}" in its title or H1', [], f'None of the {len(ok)} audited pages has "{keyword}" in its title or H1.', f'Pick the page that best answers "{keyword}" and put the phrase in its title and H1.', "quick fix"))

    # Headings and content
    no_h1 = [p for p in ok if p["h1_count"] == 0]
    if no_h1:
        found.append(finding("h1-missing", "headings_content", "high", "Pages have no H1 heading", where(no_h1), ", ".join(short(p["final_url"]) for p in no_h1[:5]), "Add one H1 per page that states the page's topic in plain words.", "quick fix"))
    many_h1 = [p for p in ok if p["h1_count"] > 1]
    if many_h1:
        found.append(finding("h1-multiple", "headings_content", "low", "Pages have more than one H1", where(many_h1), "; ".join(f'{short(p["final_url"])}: {p["h1_count"]}' for p in many_h1[:5]), "Keep one H1 and turn the others into H2s.", "quick fix"))
    thin = [p for p in ok if p["final_url"] != home_url and 0 < p["word_count"] < 300]
    if thin:
        found.append(finding("thin-content", "headings_content", "medium", "Pages have under 300 words of content", where(thin), "; ".join(f'{short(p["final_url"])}: {p["word_count"]} words' for p in thin[:5]), "Add the specifics a searcher needs (what it is, who it is for, how it works, proof, FAQ).", "project"))
    empty = [p for p in ok if p["word_count"] == 0]
    if empty:
        found.append(finding("no-html-text", "headings_content", "critical", "Pages show no text without JavaScript", where(empty), ", ".join(short(p["final_url"]) for p in empty[:5]), "Server-render or pre-render the main content so crawlers that do not run JavaScript can read it.", "project"))

    # Structured data and social
    if home and home["status"] == 200 and not home["json_ld_types"]:
        found.append(finding("home-no-schema", "structured_social", "medium", "Homepage has no structured data", [home["final_url"]], "No JSON-LD block on the homepage.", "Add Organization (and WebSite) JSON-LD with the company's name, URL and logo.", "quick fix"))
    invalid = [p for p in ok if p["json_ld_invalid"]]
    if invalid:
        found.append(finding("schema-invalid", "structured_social", "high", "JSON-LD blocks are not valid JSON", where(invalid), ", ".join(short(p["final_url"]) for p in invalid[:5]), "Fix the JSON syntax; search engines ignore a block they cannot parse.", "quick fix"))
    no_og = [p for p in ok if not (p["open_graph"]["title"] and p["open_graph"]["image"])]
    if no_og:
        found.append(finding("open-graph-missing", "structured_social", "low", "Pages lack Open Graph title or image", where(no_og), f"{len(no_og)} of {len(ok)} pages.", "Add og:title, og:description and og:image so shared links show a proper card.", "quick fix"))

    # Internal links and images
    orphans = [p for p in ok if p["final_url"] != home_url and p["inbound_internal_links"] == 0]
    if orphans and len(ok) > 2:
        found.append(finding("weak-internal-links", "internal_links", "medium", "Pages no other audited page links to", where(orphans), ", ".join(short(p["final_url"]) for p in orphans[:5]), "Link to them from related pages with descriptive anchor text.", "quick fix"))
    alt = [p for p in ok if p["images_missing_alt"]]
    if alt:
        total = sum(p["images_missing_alt"] for p in alt)
        found.append(finding("images-no-alt", "internal_links", "low", "Images have no alt text", where(alt), f"{total} images across {len(alt)} pages.", "Describe each meaningful image in its alt attribute.", "quick fix"))

    # Technical
    if not site["https_redirect"]:
        found.append(finding("no-https-redirect", "technical", "high", "http:// does not redirect to https://", [], f'http://{site["host"]}/ answered without redirecting to https.', "Redirect all http traffic to https with a 301.", "quick fix"))
    slow = [p for p in ok if p["response_ms"] > 2000]
    if slow:
        found.append(finding("slow-response", "technical", "low", "Pages take over 2 seconds to respond", where(slow), "; ".join(f'{short(p["final_url"])}: {p["response_ms"]} ms' for p in slow[:5]), "Cache or speed up the server response for these pages.", "project"))
    no_lang = [p for p in ok if not p["lang"]]
    if no_lang:
        found.append(finding("lang-missing", "technical", "low", "Pages do not declare their language", where(no_lang), f"{len(no_lang)} of {len(ok)} pages have no <html lang>.", 'Set <html lang="en"> (or the right language) in the page template.', "quick fix"))

    found.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], -len(f["pages"]), f["id"]))
    return found


def spread(urls: list[str], keep_first: int) -> list[str]:
    """Take pages section by section (/blog, /docs, /solutions...) so one big section cannot
    fill the whole sample, with legal, login and careers pages last. The first `keep_first`
    URLs (asked for by name) keep their place."""
    fixed, rest = urls[:keep_first], urls[keep_first:]
    low = [u for u in rest if LOW_VALUE.search(u)]
    sections: dict[str, list[str]] = {}
    for url in (u for u in rest if not LOW_VALUE.search(u)):
        segment = (urlsplit(url).path.strip("/").split("/") or [""])[0]
        sections.setdefault(segment, []).append(url)
    ordered: list[str] = []
    while any(sections.values()):
        for segment in list(sections):
            if sections[segment]:
                ordered.append(sections[segment].pop(0))
    return fixed + ordered + low


def error_output(site_url: str, message: str) -> dict[str, Any]:
    return {
        "outcome": "error",
        "site": site_url,
        "robots": {"status": None, "sitemaps": [], "blocks_all": False},
        "sitemap": {"url": None, "status": None, "url_count": 0},
        "pages": [],
        "findings": [],
        "categories": [{"id": cid, "name": name, "score": None} for cid, name in CATEGORIES],
        "overall_score": None,
        "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "target_urls": [],
        "sentence": "",
        "unknowns": [message],
    }


def build(inputs: dict[str, Any]) -> tuple[dict[str, Any], int]:
    raw = str(inputs.get("site_url") or "").strip()
    if not raw:
        return error_output("", "site_url is missing."), 1
    if "//" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    if not parts.hostname:
        return error_output(raw, "site_url is not a URL."), 1
    host = parts.hostname.lower().removeprefix("www.")
    origin = f"{parts.scheme or 'https'}://{parts.netloc}"
    max_pages = inputs.get("max_pages") if isinstance(inputs.get("max_pages"), int) else 15
    max_pages = max(1, min(max_pages, 30))
    keywords_raw = inputs.get("focus_keywords")
    keywords = [k.strip() for k in (re.split(r"[\n,]+", keywords_raw) if isinstance(keywords_raw, str) else keywords_raw or []) if isinstance(k, str) and k.strip()][:10]
    extra = [u for u in (inputs.get("pages") or []) if isinstance(u, str) and same_site(u, host)]

    unknowns: list[str] = ["Pages were read as raw HTML without running JavaScript, as most crawlers other than Google see them."]

    home_result = fetch(origin + "/")
    if home_result["status"] == 0:
        return error_output(origin, f"The homepage could not be reached ({home_result['error']}), so nothing was audited."), 1
    origin = f"{urlsplit(home_result['final_url']).scheme}://{urlsplit(home_result['final_url']).netloc}"

    robots = fetch(origin + "/robots.txt")
    declared, disallow, allow = robots_rules(robots["text"]) if robots["status"] == 200 else ([], [], [])
    if robots["status"] != 200:
        unknowns.append(f"robots.txt returned {robots['status'] or robots['error']}, so no crawl rules were applied.")
    blocks_all = any(rule == "/" for rule in disallow) and not any(rule == "/" for rule in allow)

    sitemap_list, sitemap_info = sitemap_urls(declared or [origin + "/sitemap.xml"], host)
    sitemap_set = {normalize(u) for u in sitemap_list}

    http_check = fetch(f"http://{urlsplit(origin).netloc}/", follow=False)
    location = http_check["headers"].get("location", "")
    https_redirect = http_check["status"] in (301, 302, 307, 308) and location.startswith("https://") or http_check["status"] == 0
    if http_check["status"] == 0:
        unknowns.append("http:// could not be reached, so the https redirect was not checked.")

    candidates: list[str] = []
    for url in extra + sorted(sitemap_list, key=lambda u: (bool(LOW_VALUE.search(u)), not KEY_PATHS.search(u), urlsplit(u).path.count("/"), len(u), u)):
        if not SKIP_EXT.search(urlsplit(url).path) and normalize(url) != normalize(home_result["final_url"]):
            candidates.append(url)
    if len(candidates) < max_pages - 1:
        home_page = audit_page(home_result, host, sitemap_set, disallow, allow)
        for link in sorted(home_page["internal_links_out"], key=lambda u: (bool(LOW_VALUE.search(u)), not KEY_PATHS.search(u), u.count("/"), u)):
            if not SKIP_EXT.search(urlsplit(link).path) and normalize(link) != normalize(home_result["final_url"]):
                candidates.append(link)
    queue = spread(list(dict.fromkeys(normalize(u) for u in candidates)), len(extra))[: max_pages - 1]

    pages = [audit_page(home_result, host, sitemap_set, disallow, allow)]
    for url in queue:
        time.sleep(PAUSE)
        pages.append(audit_page(fetch(url), host, sitemap_set, disallow, allow))

    audited = {normalize(p["final_url"]): p for p in pages}
    for page in pages:
        for link in page["internal_links_out"]:
            target = audited.get(link)
            if target is not None and target is not page:
                target["inbound_internal_links"] += 1
    home = pages[0]

    site = {
        "host": urlsplit(origin).netloc,
        "robots_blocks_all": blocks_all,
        "sitemap_url": sitemap_info["url"],
        "sitemap_status": sitemap_info["status"],
        "sitemap_declared": bool(declared),
        "https_redirect": https_redirect,
    }
    found = findings_for(pages, home, site, keywords)

    categories = []
    for cid, name in CATEGORIES:
        lost = sum(SEVERITY_POINTS[f["severity"]] for f in found if f["category"] == cid)
        categories.append({"id": cid, "name": name, "score": max(0, int(round(10 - lost)))})
    overall = int(round(sum(c["score"] for c in categories) / len(categories) * 10))
    counts = {level: sum(1 for f in found if f["severity"] == level) for level in SEVERITY_ORDER}

    if sitemap_info["url_count"] > len(pages):
        unknowns.append(f"{len(pages)} of {sitemap_info['url_count']} sitemap URLs were audited; issues on other pages are not shown.")
    if not keywords:
        unknowns.append("No focus keywords were given, so keyword targeting was not checked.")
    unknowns.append("Rankings, clicks and search volume are not part of this audit: they need Search Console or an SEO data provider.")

    for page in pages:
        page["internal_links_out"] = len(page["internal_links_out"])

    tally = ", ".join(f"{counts[level]} {level}" for level in SEVERITY_ORDER if counts[level])
    sentence = f"On-page SEO {overall}/100 across {len(pages)} pages: {tally or 'no issues found'}."
    target_urls = list(dict.fromkeys(url for f in found for url in f["pages"]))[:15]
    output = {
        "outcome": "ok" if all(p["status"] == 200 for p in pages) else "partial",
        "site": origin,
        "robots": {"status": robots["status"] or None, "sitemaps": declared, "blocks_all": blocks_all},
        "sitemap": sitemap_info,
        "pages": pages,
        "findings": found,
        "categories": categories,
        "overall_score": overall,
        "counts": counts,
        "target_urls": target_urls,
        "sentence": sentence,
        "unknowns": unknowns,
    }
    return output, 0


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
        output, code = error_output("", "The inputs could not be read."), 1
    text = json.dumps(output, ensure_ascii=False)
    try:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        print(f"could not write {args.out}: {type(exc).__name__}", file=sys.stderr)
    print(text)
    return code


def read_inputs(path: str | None) -> dict[str, Any]:
    if path:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    file_path = os.environ.get("INPUT_JSON_PATH")
    if file_path:
        with open(file_path, encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(os.environ.get("INPUT_JSON") or "{}")


if __name__ == "__main__":
    raise SystemExit(main())
