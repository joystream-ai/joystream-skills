#!/usr/bin/env python3
"""Measure a website's page speed and Core Web Vitals, deterministically.

Run inside JoyStream's sandbox by the website-seo-performance skill:

    python3 scripts/measure.py

Inputs come from INPUT_JSON or, when that is over 64 KiB, the file named by
INPUT_JSON_PATH. `--input FILE` reads them from a file instead (tests, local runs).

Standard library only. Runs Lighthouse (LIGHTHOUSE_BIN, else `lighthouse` on PATH) once per
page for lab data, and asks the PageSpeed Insights API (PSI_ENDPOINT, PSI_API_KEY) for the
Chrome UX Report field data of the site, best effort. Prints exactly one JSON object on
stdout, valid against assets/output.schema.json, and writes the same object to
/tmp/website-seo-performance.json. When nothing could be measured it still prints a valid
object and exits 1.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

DEFAULT_OUT = "/tmp/website-seo-performance.json"
PSI_DEFAULT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
USER_AGENT = "Mozilla/5.0 (compatible; JoyStreamSEOAudit/1.0; +https://joystream.ai)"
PAGE_TIMEOUT = 120
PSI_TIMEOUT = 90
# One sandbox command is killed at 300 s: stop starting pages after this, and report the rest.
BUDGET_S = 240
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
CATEGORY_KEYS = {"performance": "performance_score", "seo": "seo_score", "accessibility": "accessibility_score", "best-practices": "best_practices_score"}
# (good at or below, poor above), Google's Core Web Vitals thresholds.
THRESHOLDS = {"lcp": (2500, 4000), "cls": (0.1, 0.25), "tbt": (200, 600), "inp": (200, 500)}
RENDER_BLOCKING = ("render-blocking-resources", "render-blocking-insight")
IMAGES = ("uses-optimized-images", "modern-image-formats", "uses-responsive-images", "offscreen-images", "image-delivery-insight")
UNUSED_JS = ("unused-javascript",)


# ---------------------------------------------------------------- lab data


def lighthouse_binary() -> str | None:
    configured = os.environ.get("LIGHTHOUSE_BIN")
    if configured:
        return configured if os.path.isfile(configured) and os.access(configured, os.X_OK) else None
    return shutil.which("lighthouse")


def run_lighthouse(binary: str, url: str, strategy: str, timeout: float) -> tuple[dict[str, Any] | None, str | None]:
    """(Lighthouse result, None) or (None, why it failed)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "lhr.json")
        command = [
            binary,
            url,
            "--output=json",
            f"--output-path={path}",
            "--quiet",
            "--only-categories=performance,seo,accessibility,best-practices",
            "--max-wait-for-load=45000",
            "--chrome-flags=--headless=new --no-sandbox --disable-dev-shm-usage",
        ]
        if strategy == "desktop":
            command.append("--preset=desktop")
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return None, f"Lighthouse timed out after {int(timeout)} s"
        except OSError as exc:
            return None, f"Lighthouse could not start ({type(exc).__name__})"
        if done.returncode != 0 or not os.path.exists(path):
            last = (done.stderr or "").strip().splitlines()[-1:] or [""]
            return None, f"Lighthouse exited {done.returncode}: {last[0][:200]}".rstrip(": ")
        try:
            with open(path, encoding="utf-8") as handle:
                lhr = json.load(handle)
        except (OSError, ValueError):
            return None, "Lighthouse wrote no readable report"
    if not isinstance(lhr, dict):
        return None, "Lighthouse wrote no readable report"
    error = lhr.get("runtimeError")
    if isinstance(error, dict) and error.get("code") and error.get("code") != "NO_ERROR":
        return None, f'{error.get("code")}: {str(error.get("message") or "")[:200]}'.rstrip(": ")
    return lhr, None


def number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def rating(metric: str, value: float | None) -> str | None:
    if value is None:
        return None
    good, poor = THRESHOLDS[metric]
    return "good" if value <= good else "needs_improvement" if value <= poor else "poor"


def savings(audit: dict[str, Any]) -> tuple[int, int]:
    """(milliseconds, bytes) an audit says could be saved; Lighthouse 12 opportunities and
    Lighthouse 13 insights both."""
    details = audit.get("details") if isinstance(audit.get("details"), dict) else {}
    ms = number(details.get("overallSavingsMs"))
    if ms is None:
        metric_savings = audit.get("metricSavings") if isinstance(audit.get("metricSavings"), dict) else {}
        values = [number(metric_savings.get(k)) for k in ("LCP", "FCP")]
        ms = max((v for v in values if v is not None), default=0.0)
    size = number(details.get("overallSavingsBytes")) or 0.0
    return int(round(ms)), int(round(size))


def read_page(url: str, lhr: dict[str, Any] | None, error: str | None) -> dict[str, Any]:
    page: dict[str, Any] = {
        "url": url,
        "status": "error",
        "error": error,
        "performance_score": None,
        "seo_score": None,
        "accessibility_score": None,
        "best_practices_score": None,
        "lcp_ms": None,
        "cls": None,
        "tbt_ms": None,
        "fcp_ms": None,
        "speed_index_ms": None,
        "ratings": {"lcp": None, "cls": None, "tbt": None},
        "opportunities": [],
        "_audits": {},
    }
    if lhr is None:
        return page
    categories = lhr.get("categories") if isinstance(lhr.get("categories"), dict) else {}
    for key, field in CATEGORY_KEYS.items():
        score = number((categories.get(key) or {}).get("score")) if isinstance(categories.get(key), dict) else None
        page[field] = int(round(score * 100)) if score is not None else None
    if page["performance_score"] is None:
        page["error"] = "Lighthouse returned no performance score"
        return page
    audits = lhr.get("audits") if isinstance(lhr.get("audits"), dict) else {}
    audits = {k: v for k, v in audits.items() if isinstance(v, dict)}

    def metric(audit_id: str) -> float | None:
        return number(audits.get(audit_id, {}).get("numericValue"))

    def ms(audit_id: str) -> int | None:
        value = metric(audit_id)
        return int(round(value)) if value is not None else None

    cls = metric("cumulative-layout-shift")
    page.update(
        status="ok",
        error=None,
        lcp_ms=ms("largest-contentful-paint"),
        cls=round(cls, 3) if cls is not None else None,
        tbt_ms=ms("total-blocking-time"),
        fcp_ms=ms("first-contentful-paint"),
        speed_index_ms=ms("speed-index"),
    )
    page["ratings"] = {"lcp": rating("lcp", page["lcp_ms"]), "cls": rating("cls", page["cls"]), "tbt": rating("tbt", page["tbt_ms"])}
    opportunities = []
    for audit_id, audit in audits.items():
        details = audit.get("details") if isinstance(audit.get("details"), dict) else {}
        if details.get("type") != "opportunity" and not audit_id.endswith("-insight"):
            continue
        if audit.get("score") == 1:
            continue
        saved_ms, saved_bytes = savings(audit)
        if saved_ms > 0 or saved_bytes > 0:
            opportunities.append({"id": audit_id, "title": str(audit.get("title") or audit_id)[:200], "savings_ms": saved_ms, "savings_bytes": saved_bytes})
    opportunities.sort(key=lambda o: (-o["savings_ms"], -o["savings_bytes"], o["id"]))
    page["opportunities"] = opportunities[:5]
    page["_audits"] = {
        "server_ms": ms("server-response-time"),
        "render_blocking": [savings(audits[a]) for a in RENDER_BLOCKING if a in audits],
        "images": [savings(audits[a]) for a in IMAGES if a in audits],
        "unused_js": [savings(audits[a]) for a in UNUSED_JS if a in audits],
    }
    return page


# ---------------------------------------------------------------- field data


def fetch_field(url: str, strategy: str) -> tuple[dict[str, Any] | None, str | None]:
    """(field data, None) or (None, why there is none). Never raises."""
    query = {"url": url, "strategy": strategy, "category": "performance"}
    key = os.environ.get("PSI_API_KEY")
    if key:
        query["key"] = key
    request = urllib.request.Request(f'{os.environ.get("PSI_ENDPOINT") or PSI_DEFAULT}?{urlencode(query)}', headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=PSI_TIMEOUT) as response:
            payload = json.loads(response.read(5_000_000).decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return None, f"the PageSpeed API refused the request ({exc.code})"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return None, f"the PageSpeed API could not be reached ({type(exc).__name__})"
    if not isinstance(payload, dict):
        return None, "the PageSpeed API returned nothing readable"
    for source, block in (("crux_origin", payload.get("originLoadingExperience")), ("crux_url", payload.get("loadingExperience"))):
        metrics = block.get("metrics") if isinstance(block, dict) else None
        if not isinstance(metrics, dict) or not metrics:
            continue

        def percentile(name: str) -> float | None:
            entry = metrics.get(name)
            return number(entry.get("percentile")) if isinstance(entry, dict) else None

        lcp, inp, cls = percentile("LARGEST_CONTENTFUL_PAINT_MS"), percentile("INTERACTION_TO_NEXT_PAINT"), percentile("CUMULATIVE_LAYOUT_SHIFT_SCORE")
        cls = round(cls / 100, 3) if cls is not None else None
        overall = block.get("overall_category")
        field = {
            "source": source,
            "lcp_ms": int(round(lcp)) if lcp is not None else None,
            "inp_ms": int(round(inp)) if inp is not None else None,
            "cls": cls,
            "overall": overall if overall in ("FAST", "AVERAGE", "SLOW") else None,
            "ratings": {"lcp": rating("lcp", lcp), "inp": rating("inp", inp), "cls": rating("cls", cls)},
        }
        return field, None
    return None, "the Chrome UX Report has no data for this site (too little traffic)"


# ---------------------------------------------------------------- findings


def short(url: str) -> str:
    return urlsplit(url).path or "/"


def seconds(ms: int) -> str:
    return f"{ms / 1000:.1f} s"


def finding(fid: str, severity: str, title: str, pages: list[str], evidence: str, fix: str, effort: str) -> dict[str, Any]:
    return {"id": fid, "category": "performance", "severity": severity, "title": title, "pages": pages, "evidence": evidence, "fix": fix, "effort": effort}


def findings_for(pages: list[dict[str, Any]], field: dict[str, Any] | None, home: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    ok = [p for p in pages if p["status"] == "ok"]

    def vital(fid: str, key: str, value_key: str, title: str, show: Any, fix: str) -> None:
        hit = [p for p in ok if p["ratings"][key] in ("poor", "needs_improvement")]
        if not hit:
            return
        hit.sort(key=lambda p: (p["ratings"][key] != "poor", -p[value_key], p["url"]))
        severity = "high" if any(p["ratings"][key] == "poor" for p in hit) else "medium"
        evidence = "; ".join(f'{short(p["url"])}: {show(p[value_key])} ({p["ratings"][key].replace("_", " ")})' for p in hit[:5])
        found.append(finding(fid, severity, title, [p["url"] for p in hit], evidence, fix, "project"))

    vital("lcp-slow", "lcp", "lcp_ms", "The main content loads slowly (Largest Contentful Paint)", seconds,
          "Make the largest element (usually the hero image or headline) load first: preload it, compress it, and cut render-blocking CSS and JavaScript.")
    vital("cls-unstable", "cls", "cls", "The layout shifts while the page loads (Cumulative Layout Shift)", lambda v: f"{v:g}",
          "Reserve space for images, embeds and banners (width and height, or aspect-ratio) and never insert content above what is already shown.")
    vital("tbt-blocking", "tbt", "tbt_ms", "JavaScript blocks the page from responding (Total Blocking Time)", lambda v: f"{v} ms",
          "Split, defer or remove JavaScript that runs on load, third-party scripts first.")

    if field and field["overall"] in ("SLOW", "AVERAGE"):
        parts = []
        for label, key, show in (("LCP", "lcp_ms", seconds), ("INP", "inp_ms", lambda v: f"{v} ms"), ("CLS", "cls", lambda v: f"{v:g}")):
            if field[key] is not None:
                state = field["ratings"][key.split("_")[0]]
                parts.append(f"{label} {show(field[key])} ({state.replace('_', ' ')})")
        scope = "the whole site" if field["source"] == "crux_origin" else "the homepage"
        found.append(finding(
            "field-cwv-failing", "critical" if field["overall"] == "SLOW" else "high",
            "Real visitors get a slow experience (Chrome UX Report)", [home],
            f"Chrome users on {scope}, last 28 days, 75th percentile: " + ", ".join(parts) + f"; overall {field['overall']}.",
            "Fix the lab findings for the pages with the most traffic first; field data catches up over 28 days.", "project",
        ))

    def heavy(fid: str, key: str, title: str, fix: str, effort: str, min_bytes: int | None = None) -> None:
        hit = []
        for p in ok:
            ms = sum(s[0] for s in p["_audits"][key])
            size = sum(s[1] for s in p["_audits"][key])
            if ms >= 500 or (min_bytes is not None and size >= min_bytes):
                hit.append((p, ms, size))
        if not hit:
            return
        hit.sort(key=lambda h: (-h[1], -h[2], h[0]["url"]))
        evidence = "; ".join(f'{short(p["url"])}: about {seconds(ms)}' + (f", {size // 1024} KB" if size else "") + " to save" for p, ms, size in hit[:5])
        found.append(finding(fid, "medium", title, [p["url"] for p, _, _ in hit], evidence, fix, effort))

    heavy("render-blocking", "render_blocking", "CSS and JavaScript delay the first paint",
          "Inline the critical CSS, and load the rest of the CSS and scripts with defer or async.", "project")
    heavy("images-heavy", "images", "Images are larger than they need to be",
          "Serve images as WebP or AVIF at the size they are shown, and lazy-load those below the fold.", "quick fix", min_bytes=200 * 1024)
    heavy("unused-js", "unused_js", "Pages load JavaScript they do not use",
          "Remove unused libraries and split the bundle so each page loads only its own code.", "project")

    slow = sorted((p for p in ok if (p["_audits"]["server_ms"] or 0) > 600), key=lambda p: (-p["_audits"]["server_ms"], p["url"]))
    if slow:
        found.append(finding("slow-server", "medium", "The server takes over 600 ms to send the page", [p["url"] for p in slow],
                             "; ".join(f'{short(p["url"])}: {p["_audits"]["server_ms"]} ms' for p in slow[:5]),
                             "Cache the HTML at a CDN, or speed up the server work behind these pages.", "project"))
    low = sorted((p for p in ok if p["performance_score"] < 50), key=lambda p: (p["performance_score"], p["url"]))
    if low:
        found.append(finding("perf-score-low", "high", "Lighthouse performance score is under 50", [p["url"] for p in low],
                             "; ".join(f'{short(p["url"])}: {p["performance_score"]}/100' for p in low[:5]),
                             "Work through the slow-loading findings above, starting with the largest savings.", "project"))

    found.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], -len(f["pages"]), f["id"]))
    return found


# ---------------------------------------------------------------- build


def same_site(url: str, host: str) -> bool:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.") == host


def error_output(site: str, strategy: str, message: str, pages: list[dict[str, Any]] | None = None, field: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "outcome": "error",
        "site": site,
        "strategy": strategy,
        "pages": pages or [],
        "field": field,
        "findings": [],
        "score": None,
        "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "target_urls": [],
        "sentence": "",
        "unknowns": [message],
    }


def build(inputs: dict[str, Any]) -> tuple[dict[str, Any], int]:
    started = time.monotonic()
    strategy = "desktop" if inputs.get("strategy") == "desktop" else "mobile"
    raw = str(inputs.get("site_url") or "").strip()
    if not raw:
        return error_output("", strategy, "site_url is missing."), 1
    if "//" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    if not parts.hostname:
        return error_output(raw, strategy, "site_url is not a URL."), 1
    host = parts.hostname.lower().removeprefix("www.")
    origin = f"{parts.scheme or 'https'}://{parts.netloc}"
    home = origin + "/"
    max_pages = inputs.get("max_pages") if isinstance(inputs.get("max_pages"), int) and not isinstance(inputs.get("max_pages"), bool) else 3
    max_pages = max(1, min(max_pages, 5))
    field_wanted = inputs.get("field_data") if isinstance(inputs.get("field_data"), bool) else True
    extra = [u.strip() for u in (inputs.get("pages") or []) if isinstance(u, str) and u.strip().startswith(("http://", "https://")) and same_site(u.strip(), host)]
    urls = list(dict.fromkeys([home] + extra))[:max_pages]

    binary = lighthouse_binary()
    if binary is None:
        return error_output(origin, strategy, "Lighthouse is not installed here (set LIGHTHOUSE_BIN or put lighthouse on PATH), so nothing was measured."), 1

    field_result: dict[str, Any] = {"field": None, "why": "field data was not requested"}
    worker = None
    if field_wanted:
        def ask() -> None:
            field_result["field"], field_result["why"] = fetch_field(home, strategy)

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()

    pages: list[dict[str, Any]] = []
    skipped: list[str] = []
    for url in urls:
        left = BUDGET_S - (time.monotonic() - started)
        if left < 30:
            skipped.append(url)
            continue
        lhr, error = run_lighthouse(binary, url, strategy, min(PAGE_TIMEOUT, left))
        pages.append(read_page(url, lhr, error))

    if worker is not None:
        worker.join(max(1.0, BUDGET_S + 30 - (time.monotonic() - started)))
        if worker.is_alive():
            field_result = {"field": None, "why": "the PageSpeed API did not answer in time"}
    field = field_result["field"]

    device = "a throttled mid-range phone on a slow 4G connection" if strategy == "mobile" else "a desktop on a fast connection"
    unknowns = [f"Lab data is one simulated Lighthouse run per page on {device}; scores can move 5-10 points between runs."]
    if field is None:
        unknowns.append(f"No real-user data: {field_result['why']}, so this is lab data only.")
    unknowns.append("Interaction to Next Paint needs real users; in the lab, Total Blocking Time stands in for it.")
    if skipped:
        unknowns.append(f"{len(skipped)} page(s) were not measured, to stay within the step's time limit: " + ", ".join(short(u) for u in skipped) + ".")

    ok = [p for p in pages if p["status"] == "ok"]
    if not ok:
        first = next((p["error"] for p in pages if p["error"]), "no page was measured")
        for page in pages:
            page.pop("_audits", None)
        return error_output(origin, strategy, f"Lighthouse could not measure any page ({first}).", pages, field), 1

    found = findings_for(pages, field, home)
    for page in pages:
        page.pop("_audits", None)
    score = int(round(sum(p["performance_score"] for p in ok) / len(ok)))
    counts = {level: sum(1 for f in found if f["severity"] == level) for level in SEVERITY_ORDER}

    sentence = f"{strategy.capitalize()} performance {score}/100 across {len(ok)} page{'s' if len(ok) != 1 else ''}"
    worst = max((p for p in ok if p["lcp_ms"] is not None), key=lambda p: (p["lcp_ms"], p["url"]), default=None)
    if worst and worst["ratings"]["lcp"] != "good":
        sentence += f": LCP {seconds(worst['lcp_ms'])} on {short(worst['url'])}."
    elif not found:
        sentence += ": every lab Core Web Vital is in the good range."
    else:
        sentence += f": {', '.join(f'{counts[level]} {level}' for level in SEVERITY_ORDER if counts[level])}."
    if field and field["overall"]:
        sentence += f" Real users: {field['overall'].lower()}."
    if len(ok) < len(pages):
        sentence += f" {len(pages) - len(ok)} page(s) could not be measured."

    output = {
        "outcome": "ok" if len(ok) == len(pages) and not skipped else "partial",
        "site": origin,
        "strategy": strategy,
        "pages": pages,
        "field": field,
        "findings": found,
        "score": score,
        "counts": counts,
        "target_urls": list(dict.fromkeys(url for f in found for url in f["pages"]))[:5],
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


# Every step of a JoyStream run shares one sandbox, and each module saves its output at
# <steps dir>/<skill>.json, so an input the model leaves out is read from there instead of
# being retyped. CLOCK_SLACK_S allows for the sandbox clock differing from the run's.
STEPS_DIR = "/tmp"
CLOCK_SLACK_S = 600
# (input, earlier module, key in its output): the pages to measure are the audit's.
EARLIER_STEPS = (("pages", "website-seo-page-audit", "target_urls"),)


def from_earlier_steps(inputs: dict[str, Any], steps_dir: str, site: Any) -> dict[str, Any]:
    """Fill each input left out from the output an earlier module saved in this sandbox.

    A saved output counts only for the same site, and only when it was written after the
    run started (the `now` input, when given): an older file belongs to another run.
    """
    started = epoch_of(inputs.get("now"))
    for field, skill, key in EARLIER_STEPS:
        if inputs.get(field) not in (None, "", [], {}):
            continue
        path = os.path.join(steps_dir, f"{skill}.json")
        try:
            if started is not None and os.path.getmtime(path) < started - CLOCK_SLACK_S:
                continue
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(saved, dict) or not same_site(saved.get("site"), site):
            continue
        value = saved if key is None else saved.get(key)
        if value not in (None, "", [], {}):
            inputs[field] = value
    return inputs


def epoch_of(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def same_site(saved: Any, site: Any) -> bool:
    """The same host, ignoring the scheme and www. With no site of our own, any matches."""

    def host(url: Any) -> str:
        if not isinstance(url, str) or not url.strip():
            return ""
        text = url.strip()
        netloc = urlsplit(text if "//" in text else f"https://{text}").netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc

    ours = host(site)
    return not ours or host(saved) == ours


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--steps-dir", default=STEPS_DIR)
    args = parser.parse_args()
    try:
        inputs = read_inputs(args.input)
        if isinstance(inputs, dict):
            inputs = from_earlier_steps(inputs, args.steps_dir, inputs.get("site_url"))
        output, code = build(inputs if isinstance(inputs, dict) else {})
    except (OSError, ValueError) as exc:
        print(f"could not read the inputs: {type(exc).__name__}", file=sys.stderr)
        output, code = error_output("", "mobile", "The inputs could not be read."), 1
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
