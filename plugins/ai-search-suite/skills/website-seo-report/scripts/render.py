#!/usr/bin/env python3
"""Render the SEO Optimizer's outputs as one polished report: an HTML page and a PDF.

Run inside JoyStream's sandbox by the website-seo-report skill:

    python3 scripts/render.py

Inputs come from INPUT_JSON or, when that is over 64 KiB, the file named by
INPUT_JSON_PATH. `--input FILE` reads them from a file instead (tests, local runs).

Standard library only. Every chart is inline SVG drawn from the inputs' numbers, and every
piece of text from the inputs is HTML-escaped. The page loads nothing: its Content Security
Policy blocks every fetch. The PDF is printed by headless Chromium (CHROME_PATH, else
`chromium` or `google-chrome` on PATH). Writes seo-report.html and seo-report.pdf to
--out-dir (default /workspace/out, the folder the run keeps), prints one JSON object on
stdout valid against assets/output.schema.json, and writes the same object to
/tmp/website-seo-report.json. Without Chromium it still writes the HTML (outcome partial);
with nothing to render it exits 1.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_OUT = "/tmp/website-seo-report.json"
DEFAULT_OUT_DIR = "/workspace/out"
BASENAME = "seo-report"
PRINT_TIMEOUT = 120

# Reference palette (dataviz skill, light mode). Status colours always sit beside a text label.
INK, INK_2, INK_3 = "#0b0b0b", "#52514e", "#8a8984"
SURFACE, RULE, TRACK = "#fcfcfb", "#e6e5e1", "#f0efec"
SERIES_1, SERIES_2 = "#2a78d6", "#eb6834"
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b", "neutral": "#a3a29c"}
SEVERITY_STATUS = {"critical": "critical", "high": "serious", "medium": "warning", "low": "neutral"}
IMPACT_STATUS = {"high": "critical", "medium": "serious", "low": "warning"}
RATING = {"good": ("good", "Good"), "needs_improvement": ("warning", "Needs work"), "poor": ("critical", "Poor")}


# ---------------------------------------------------------------- inputs


def unwrap(value: Any) -> Any:
    """A step's payload, whatever envelope it arrives in; None for a failed step."""
    for _ in range(6):
        if isinstance(value, str):
            text = value.strip()
            if not text.startswith("{"):
                return None
            try:
                value = json.loads(text)
            except ValueError:
                return None
            continue
        if not isinstance(value, dict):
            return None
        if isinstance(value.get("step"), dict):
            value = value["step"]
            continue
        if "ok" in value and ("data" in value or value.get("ok") is False):
            if value.get("ok") is False:
                return None
            value = value.get("data")
            continue
        if "output" in value and isinstance(value["output"], (dict, str)) and len(value) <= 3:
            value = value["output"]
            continue
        keys = [k for k in value if not k.startswith("_") and k not in ("artifacts", "artifact_refs")]
        if len(keys) == 1 and isinstance(value[keys[0]], dict):
            value = value[keys[0]]
            continue
        return {k: v for k, v in value.items() if k in keys}
    return value if isinstance(value, dict) else None


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(round(value))


# ---------------------------------------------------------------- text helpers


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def path_of(url: Any) -> str:
    if not isinstance(url, str) or not url:
        return ""
    parts = urlsplit(url)
    if not parts.netloc:
        return url
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def host_of(url: str) -> str:
    return urlsplit(url if "//" in url else f"https://{url}").netloc or url


def number(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else esc(value)


def seconds(ms: Any) -> str:
    return f"{ms / 1000:.1f} s" if isinstance(ms, (int, float)) else "–"


def score_status(score: int | None) -> tuple[str, str]:
    if score is None:
        return "neutral", "Not scored"
    if score >= 85:
        return "good", "Good"
    if score >= 70:
        return "warning", "Fair"
    if score >= 50:
        return "serious", "Needs work"
    return "critical", "Poor"


def chip(status: str, label: str) -> str:
    return f'<span class="chip"><i style="background:{STATUS.get(status, STATUS["neutral"])}"></i>{esc(label)}</span>'


# ---------------------------------------------------------------- charts (inline SVG)


def bar_path(x0: float, y: float, length: float, height: float, radius: float = 4) -> str:
    """A bar anchored at x0 with its data end (right) rounded."""
    x1 = x0 + max(length, 0)
    r = min(radius, height / 2, max(length, 0))
    if r <= 0:
        return f"M{x0:.1f},{y:.1f}H{x1:.1f}V{y + height:.1f}H{x0:.1f}Z"
    return (
        f"M{x0:.1f},{y:.1f}H{x1 - r:.1f}Q{x1:.1f},{y:.1f} {x1:.1f},{y + r:.1f}"
        f"V{y + height - r:.1f}Q{x1:.1f},{y + height:.1f} {x1 - r:.1f},{y + height:.1f}H{x0:.1f}Z"
    )


def hbar_chart(rows: list[dict[str, Any]], max_value: float, label: str, refs: list[tuple[float, str]] | None = None,
               label_w: int = 210, width: int = 680, bar_h: int = 14, gap: int = 12) -> str:
    """Horizontal bars. Each row: {label, value, text, color, tip, sub?}; sub rows draw a grouped bar."""
    plot_x, plot_w = label_w + 8, width - label_w - 8 - 90
    top = 22 if refs else 6
    lines: list[str] = []
    y = top
    scale = plot_w / max_value if max_value > 0 else 0
    for row in rows:
        bars = [row] + as_list(row.get("sub"))
        block_h = len(bars) * bar_h + (len(bars) - 1) * 2
        lines.append(f'<text x="{label_w}" y="{y + block_h / 2 + 4:.1f}" text-anchor="end" class="lbl">{esc(row["label"])}</text>')
        for bar in bars:
            value = bar.get("value")
            if isinstance(value, (int, float)):
                length = min(value, max_value) * scale
                lines.append(f'<path d="{bar_path(plot_x, y, length, bar_h)}" fill="{bar["color"]}"><title>{esc(bar.get("tip", ""))}</title></path>')
                lines.append(f'<text x="{plot_x + length + 6:.1f}" y="{y + bar_h - 3}" class="val">{esc(bar["text"])}</text>')
            else:
                lines.append(f'<text x="{plot_x}" y="{y + bar_h - 3}" class="muted">{esc(bar["text"])}</text>')
            y += bar_h + 2
        y += gap - 2
    height = y - gap + 8
    grid = [f'<line x1="{plot_x}" y1="{top - 4}" x2="{plot_x}" y2="{height - 4}" class="axis"/>']
    for value, ref_label in refs or []:
        x = plot_x + min(value, max_value) * scale
        grid.append(f'<line x1="{x:.1f}" y1="{top - 4}" x2="{x:.1f}" y2="{height - 4}" class="ref"/>')
        grid.append(f'<text x="{x:.1f}" y="12" text-anchor="middle" class="muted">{esc(ref_label)}</text>')
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{esc(label)}" class="chart">'
        + "".join(grid) + "".join(lines) + "</svg>"
    )


def meter(score: int | None, status: str) -> str:
    width, track_y = 300, 14
    fill = (score or 0) / 100 * width
    ticks = "".join(
        f'<line x1="{t / 100 * width:.1f}" y1="{track_y - 3}" x2="{t / 100 * width:.1f}" y2="{track_y + 13}" class="tick"/>'
        f'<text x="{t / 100 * width:.1f}" y="{track_y + 26}" text-anchor="middle" class="muted">{t}</text>'
        for t in (0, 50, 70, 85, 100)
    )
    return (
        f'<svg viewBox="-8 0 {width + 16} 44" class="meter" role="img" aria-label="Score {score} of 100">'
        f'<rect x="0" y="{track_y}" width="{width}" height="10" rx="5" fill="{TRACK}"/>'
        f'<path d="{bar_path(0, track_y, fill, 10, 5)}" fill="{STATUS[status]}"/>{ticks}</svg>'
    )


# ---------------------------------------------------------------- sections


def section(title: str, body: str, note: str = "", lead: str = "") -> str:
    """A report section; its heading, note and lead block print on the same page."""
    note_html = f'<p class="note">{note}</p>' if note else ""
    return f'<section><div class="keep"><h2>{esc(title)}</h2>{note_html}{lead}</div>{body}</section>'


def hero(site: str, score: int | None, verdict: str, audit: dict[str, Any] | None, speed: dict[str, Any] | None,
         opportunities: dict[str, Any] | None, now: str) -> str:
    status, word = score_status(score)
    changes = (audit or {}).get("changes") if isinstance(audit, dict) else None
    delta = ""
    if isinstance(changes, dict) and isinstance(changes.get("score_delta"), int):
        d = changes["score_delta"]
        arrow = "▲" if d > 0 else "▼" if d < 0 else "■"
        fixed, new = len(as_list(changes.get("fixed"))), len(as_list(changes.get("new")))
        delta = f'<div class="delta">{arrow} {d:+d} since the last audit · {fixed} fixed · {new} new</div>'
    counts = (audit or {}).get("counts") or {}
    tiles = [
        ("Critical", counts.get("critical"), "critical"),
        ("High", counts.get("high"), "serious"),
        ("Medium", counts.get("medium"), "warning"),
        ("Low", counts.get("low"), "neutral"),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="tile-n">{number(n) if isinstance(n, int) else "–"}</div>{chip(s, name)}</div>' for name, n, s in tiles
    )
    extras = []
    pages = as_list((audit or {}).get("pages"))
    if pages:
        extras.append(f"<div><b>{len(pages)}</b> pages read</div>")
    if isinstance((speed or {}).get("score"), int):
        extras.append(f"<div><b>{speed['score']}/100</b> mobile speed</div>")
    if isinstance((opportunities or {}).get("extra_clicks_total"), int):
        extras.append(f"<div><b>+{opportunities['extra_clicks_total']:,}</b> clicks / 28 days available</div>")
    score_text = f"{score}" if score is not None else "–"
    return (
        '<header class="hero">'
        f'<div class="eyebrow">SEO Optimizer report · {esc(now)}</div>'
        f'<h1>{esc(host_of(site))}</h1>'
        '<div class="hero-row">'
        f'<div class="score"><div class="score-n">{score_text}<span>/100</span></div>{chip(status, word)}{meter(score, status)}</div>'
        f'<div class="verdict"><p>{esc(verdict)}</p>{delta}<div class="extras">{"".join(extras)}</div></div>'
        "</div>"
        f'<div class="tiles">{tile_html}</div>'
        "</header>"
    )


def changes_section(actions: list[dict[str, Any]]) -> str:
    if not actions:
        return ""
    with_clicks = any(isinstance(a.get("extra_clicks"), int) for a in actions)
    rows = []
    for a in actions:
        clicks = f"<td class='num'>+{a['extra_clicks']:,}</td>" if isinstance(a.get("extra_clicks"), int) else "<td></td>" if with_clicks else ""
        rows.append(
            f"<tr><td class='num'>{esc(a.get('rank'))}</td><td class='mono'>{esc(path_of(a.get('page')))}</td>"
            f"<td>{esc(a.get('change'))}</td><td>{chip(IMPACT_STATUS.get(a.get('impact'), 'neutral'), str(a.get('impact', '')).capitalize())}</td>"
            f"<td class='nowrap'>{esc(a.get('effort'))}</td><td>{esc(a.get('owner'))}</td>{clicks}</tr>"
        )
    table = (
        "<table><thead><tr><th>#</th><th>Page</th><th>Change</th><th>Impact</th><th>Effort</th><th>Owner</th>" + ("<th>Clicks</th>" if with_clicks else "") + "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    note = "Ranked by impact. Clicks are the extra clicks per 28 days Search Console data suggests." if with_clicks else "Ranked by impact."
    return section("This week's changes", table, note)


def scorecard_section(audit: dict[str, Any] | None) -> str:
    categories = [c for c in as_list((audit or {}).get("categories")) if isinstance(c, dict) and isinstance(c.get("score"), int)]
    if not categories:
        return ""
    rows = []
    for c in categories:
        rows.append({"label": c.get("name") or c.get("id"), "value": c["score"], "text": f"{c['score']}/10", "color": SERIES_1, "tip": f"{c.get('name')}: {c['score']} of 10"})
    return section("Scorecard", "", lead=hbar_chart(rows, 10, "Category scores out of 10"), note="Each category starts at 10 and loses points per issue: critical 4, high 2, medium 1, low 0.5.")


def speed_section(speed: dict[str, Any] | None) -> str:
    if not isinstance(speed, dict):
        return ""
    pages = [p for p in as_list(speed.get("pages")) if isinstance(p, dict)]
    measured = [p for p in pages if isinstance(p.get("lcp_ms"), (int, float))]
    if not pages:
        return ""
    lead, body = "", ""
    if measured:
        rows = []
        for p in measured:
            status = RATING.get((p.get("ratings") or {}).get("lcp"), ("neutral", ""))[0]
            rows.append({"label": path_of(p.get("url")), "value": p["lcp_ms"] / 1000, "text": seconds(p["lcp_ms"]), "color": STATUS[status], "tip": f"LCP {seconds(p['lcp_ms'])}"})
        top = max(6.0, max(r["value"] for r in rows) * 1.1)
        lead = '<h3>Largest Contentful Paint per page</h3>' + hbar_chart(rows, top, "LCP per page in seconds", refs=[(2.5, "good ≤ 2.5 s"), (4.0, "poor > 4 s")])
    table_rows = []
    for p in pages:
        ratings = p.get("ratings") or {}

        def cell(key: str, text: str) -> str:
            status, word = RATING.get(ratings.get(key), ("neutral", ""))
            return f"<td>{esc(text)} {chip(status, word) if word else ''}</td>"

        if p not in measured:
            table_rows.append(f"<tr><td class='mono'>{esc(path_of(p.get('url')))}</td><td colspan='4' class='muted'>Not measured: {esc(p.get('error') or 'skipped')}</td></tr>")
            continue
        cls = p.get("cls")
        table_rows.append(
            f"<tr><td class='mono'>{esc(path_of(p.get('url')))}</td><td class='num'>{esc(p.get('performance_score'))}</td>"
            + cell("lcp", seconds(p.get("lcp_ms")))
            + cell("cls", f"{cls:.2f}" if isinstance(cls, (int, float)) else "–")
            + cell("tbt", f"{p['tbt_ms']:,} ms" if isinstance(p.get("tbt_ms"), int) else "–")
            + "</tr>"
        )
    body += "<table><thead><tr><th>Page</th><th>Score</th><th>LCP</th><th>CLS</th><th>TBT</th></tr></thead><tbody>" + "".join(table_rows) + "</tbody></table>"
    field = speed.get("field")
    if isinstance(field, dict):
        overall = {"FAST": ("good", "Fast"), "AVERAGE": ("warning", "Average"), "SLOW": ("critical", "Slow")}.get(field.get("overall"), ("neutral", "Unknown"))
        inp = f", INP {field['inp_ms']} ms" if isinstance(field.get("inp_ms"), int) else ""
        body += f'<p class="note">Real users (Chrome UX Report, 75th percentile): {chip(*overall)} LCP {seconds(field.get("lcp_ms"))}{esc(inp)}.</p>'
    else:
        body += '<p class="note">No real-user data for this site: the numbers above are one simulated mobile visit.</p>'
    return section("Speed (Core Web Vitals)", body, f"Lighthouse, {esc(speed.get('strategy') or 'mobile')}. Overall speed score {esc(speed.get('score'))}/100.", lead)


def gaps_section(gaps: dict[str, Any] | None) -> str:
    keywords = [k for k in as_list((gaps or {}).get("keywords")) if isinstance(k, dict)]
    if not keywords:
        return ""
    rows, cards = [], []
    for k in keywords:
        ours, theirs = k.get("our_words"), k.get("competitor_median_words")
        if isinstance(ours, int) or isinstance(theirs, int):
            rows.append({
                "label": k.get("keyword"),
                "value": ours, "text": f"{ours:,} words" if isinstance(ours, int) else "ours not read", "color": SERIES_1, "tip": f"Our page: {ours} words",
                "sub": [{"value": theirs, "text": f"{theirs:,} words" if isinstance(theirs, int) else "not read", "color": SERIES_2, "tip": f"Median of the top results: {theirs} words"}],
            })
        rank = k.get("our_rank")
        rank_text = f"Rank {rank}" if isinstance(rank, int) else "Not in the top results"
        rank_status = "good" if isinstance(rank, int) and rank <= 3 else "warning" if isinstance(rank, int) else "critical"
        topics = "".join(f'<span class="tag">{esc(t.get("term"))} <small>{esc(t.get("competitors"))} pages</small></span>' for t in as_list(k.get("missing_topics")) if isinstance(t, dict))
        schema = "".join(f'<span class="tag mono">{esc(s)}</span>' for s in as_list(k.get("missing_schema")))
        cards.append(
            f'<div class="card"><div class="card-head"><b>{esc(k.get("keyword"))}</b>{chip(rank_status, rank_text)}'
            f'<span class="muted mono">{esc(path_of(k.get("our_page")))}</span></div>'
            + (f'<div class="kv"><span>Topics the top results cover and we don\'t</span><div>{topics}</div></div>' if topics else "")
            + (f'<div class="kv"><span>Structured data they use and we don\'t</span><div>{schema}</div></div>' if schema else "")
            + ("" if topics or schema else '<div class="muted">No missing topics found.</div>')
            + "</div>"
        )
    chart = ""
    if rows:
        top = max([r["value"] for r in rows if isinstance(r["value"], int)] + [s["value"] for r in rows for s in r["sub"] if isinstance(s["value"], int)] + [1])
        legend = f'<div class="legend"><span><i style="background:{SERIES_1}"></i>Our page</span><span><i style="background:{SERIES_2}"></i>Top results (median)</span></div>'
        chart = "<h3>Words on the page</h3>" + legend + hbar_chart(rows, top * 1.15, "Word count, our page against the median of the top results")
    return section("Content gaps vs top results", "".join(cards), "What the pages ranking above us cover, from their headings, per focus keyword.", chart)


def opportunities_section(opportunities: dict[str, Any] | None) -> str:
    items = [o for o in as_list((opportunities or {}).get("opportunities")) if isinstance(o, dict) and isinstance(o.get("extra_clicks"), int)]
    if not items:
        return ""
    items = sorted(items, key=lambda o: -o["extra_clicks"])[:8]
    rows = [{"label": o.get("query"), "value": o["extra_clicks"], "text": f"+{o['extra_clicks']:,}", "color": SERIES_1, "tip": f"{o.get('query')}: position {o.get('position')}"} for o in items]
    return section("Search Console opportunities", "", lead=hbar_chart(rows, max(r["value"] for r in rows) * 1.15, "Extra clicks per 28 days by query"), note="Extra clicks per 28 days if the page reached its target position or click-through rate.")


def fixes_section(actions: list[dict[str, Any]]) -> str:
    blocks = []
    for a in actions:
        current, proposed = a.get("current") or {}, a.get("proposed") or {}
        parts = []
        for key, name in (("title", "Title"), ("meta_description", "Meta description"), ("h1", "H1")):
            new = proposed.get(key)
            if not new:
                continue
            old = current.get(key)
            old_html = f'<div class="old">{esc(old)} <small>{len(old)} chars</small></div>' if old else '<div class="old muted">(none)</div>'
            parts.append(f'<div class="kv"><span>{name}</span><div>{old_html}<div class="new">→ {esc(new)} <small>{len(new)} chars</small></div></div></div>')
        outline = as_list(proposed.get("section_outline"))
        if outline:
            parts.append('<div class="kv"><span>New sections</span><ol>' + "".join(f"<li>{esc(s)}</li>" for s in outline) + "</ol></div>")
        link = proposed.get("internal_link")
        if isinstance(link, dict):
            parts.append(f'<div class="kv"><span>Internal link</span><div>From <span class="mono">{esc(path_of(link.get("from_page")))}</span> with the text “{esc(link.get("anchor_text"))}”</div></div>')
        if proposed.get("json_ld"):
            parts.append(f'<div class="kv"><span>JSON-LD</span><pre>{esc(proposed["json_ld"])}</pre></div>')
        if not parts:
            continue
        blocks.append(
            f'<div class="card"><div class="card-head"><b>{esc(a.get("rank"))}. {esc(a.get("change"))}</b>'
            f'<span class="muted mono">{esc(path_of(a.get("page")))}</span></div><p class="why">{esc(a.get("why"))}</p>{"".join(parts)}</div>'
        )
    return section("Copy-ready fixes", "".join(blocks[1:]), "Drafts to check before publishing.", blocks[0]) if blocks else ""


def findings_section(modules: list[dict[str, Any] | None]) -> str:
    findings = [f for m in modules if isinstance(m, dict) for f in as_list(m.get("findings")) if isinstance(f, dict)]
    if not findings:
        return ""
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (order.get(f.get("severity"), 4), str(f.get("id"))))
    rows = []
    for f in findings:
        pages = as_list(f.get("pages"))
        where = ", ".join(path_of(p) for p in pages[:4]) + (f" +{len(pages) - 4} more" if len(pages) > 4 else "")
        rows.append(
            f"<tr><td>{chip(SEVERITY_STATUS.get(f.get('severity'), 'neutral'), str(f.get('severity', '')).capitalize())}</td>"
            f"<td><b>{esc(f.get('title'))}</b><div class='muted'>{esc(f.get('evidence'))}</div><div>{esc(f.get('fix'))}</div></td>"
            f"<td class='mono small'>{esc(where)}</td></tr>"
        )
    return section("All findings", f"<table class='findings'><thead><tr><th>Severity</th><th>Issue and fix</th><th>Pages</th></tr></thead><tbody>{''.join(rows)}</tbody></table>")


def unknowns_section(lines: list[str]) -> str:
    if not lines:
        return ""
    return section("What this report could not check", '<ul class="unknowns">' + "".join(f"<li>{esc(u)}</li>" for u in lines) + "</ul>")


# ---------------------------------------------------------------- page

CSS = f"""
@page {{ size: A4; margin: 14mm 13mm; }}
* {{ box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{ margin: 0; background: {SURFACE}; color: {INK}; font: 12.5px/1.5 -apple-system, "Segoe UI", "Helvetica Neue", "DejaVu Sans", "Liberation Sans", Arial, sans-serif; }}
main {{ max-width: 760px; margin: 0 auto; padding: 28px 20px; }}
h1 {{ font-size: 26px; margin: 2px 0 14px; letter-spacing: -0.01em; }}
h2 {{ font-size: 16px; margin: 0 0 4px; padding-top: 6px; border-top: 2px solid {INK}; }}
h3 {{ font-size: 12.5px; margin: 12px 0 4px; color: {INK_2}; }}
section {{ margin: 26px 0 0; }}
.keep {{ break-inside: avoid; }}
h2, h3 {{ break-after: avoid; }}
.eyebrow, .note, .muted {{ color: {INK_2}; }}
.note {{ margin: 0 0 10px; font-size: 11.5px; }}
.hero {{ border: 1px solid {RULE}; border-radius: 10px; padding: 18px 20px; background: #fff; break-inside: avoid; }}
.hero-row {{ display: flex; gap: 24px; align-items: flex-start; }}
.score {{ flex: 0 0 300px; }}
.score-n {{ font-size: 52px; font-weight: 700; line-height: 1; letter-spacing: -0.02em; }}
.score-n span {{ font-size: 18px; color: {INK_2}; font-weight: 500; }}
.score .chip {{ margin: 6px 0 4px; }}
.meter {{ width: 300px; display: block; }}
.meter .tick {{ stroke: {INK_3}; stroke-width: 1; }}
.verdict p {{ font-size: 14px; margin: 0 0 8px; }}
.delta {{ font-weight: 600; margin-bottom: 8px; }}
.extras {{ display: flex; flex-wrap: wrap; gap: 6px 18px; color: {INK_2}; }}
.extras b {{ color: {INK}; }}
.tiles {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 16px; }}
.tile {{ border: 1px solid {RULE}; border-radius: 8px; padding: 8px 10px; }}
.tile-n {{ font-size: 22px; font-weight: 700; }}
.chip {{ display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: {INK}; white-space: nowrap; }}
.chip i {{ width: 9px; height: 9px; border-radius: 50%; display: inline-block; }}
table {{ width: 100%; border-collapse: collapse; font-size: 11.5px; }}
th {{ text-align: left; color: {INK_2}; font-weight: 600; border-bottom: 1px solid {INK_3}; padding: 5px 6px; }}
td {{ border-bottom: 1px solid {RULE}; padding: 6px; vertical-align: top; }}
tr {{ break-inside: avoid; }}
.nowrap {{ white-space: nowrap; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.mono {{ font-family: "SF Mono", Menlo, "DejaVu Sans Mono", Consolas, monospace; font-size: 11px; }}
.small {{ font-size: 10.5px; }}
.chart {{ width: 100%; height: auto; display: block; margin: 4px 0 10px; }}
.chart .lbl {{ font-size: 11px; fill: {INK}; }}
.chart .val {{ font-size: 11px; fill: {INK}; font-variant-numeric: tabular-nums; }}
.chart .muted, .meter .muted {{ font-size: 10px; fill: {INK_2}; }}
.chart .axis {{ stroke: {INK_3}; stroke-width: 1; }}
.chart .ref {{ stroke: {INK_3}; stroke-width: 1; stroke-dasharray: 3 3; }}
.legend {{ display: flex; gap: 16px; font-size: 11px; color: {INK_2}; }}
.legend i {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; margin-right: 5px; vertical-align: -1px; }}
.card {{ border: 1px solid {RULE}; border-radius: 8px; padding: 10px 12px; margin: 10px 0; background: #fff; break-inside: avoid; }}
.card-head {{ display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; margin-bottom: 4px; }}
.why {{ margin: 0 0 8px; color: {INK_2}; }}
.kv {{ display: grid; grid-template-columns: 150px 1fr; gap: 10px; margin: 6px 0; }}
.kv > span {{ color: {INK_2}; font-size: 11px; }}
.kv ol {{ margin: 0; padding-left: 18px; }}
.old {{ color: {INK_2}; text-decoration: line-through; text-decoration-color: {INK_3}; }}
.new {{ font-weight: 600; }}
small {{ color: {INK_3}; font-weight: 400; }}
.tag {{ display: inline-block; border: 1px solid {RULE}; border-radius: 999px; padding: 1px 8px; margin: 0 4px 4px 0; background: {TRACK}; }}
pre {{ margin: 0; padding: 8px; background: {TRACK}; border-radius: 6px; font-size: 10px; white-space: pre-wrap; word-break: break-word; }}
.unknowns li {{ margin: 3px 0; }}
footer {{ margin-top: 28px; padding-top: 8px; border-top: 1px solid {RULE}; color: {INK_2}; font-size: 11px; }}
"""


def page_html(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"
    )


# ---------------------------------------------------------------- PDF


def chrome_binary() -> str | None:
    configured = os.environ.get("CHROME_PATH")
    if configured:
        return configured if os.path.isfile(configured) and os.access(configured, os.X_OK) else None
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    return None


def print_pdf(html_path: Path, pdf_path: Path) -> str | None:
    """None when the PDF was written, else why not."""
    binary = chrome_binary()
    if not binary:
        return "Chromium was not found, so only the HTML report was made."
    with tempfile.TemporaryDirectory() as profile:
        command = [
            binary, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            "--no-first-run", "--no-pdf-header-footer", "--print-to-pdf-no-header",
            f"--user-data-dir={profile}", f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri(),
        ]
        try:
            subprocess.run(command, capture_output=True, timeout=PRINT_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Chromium could not print the PDF ({type(exc).__name__}), so only the HTML report was made."
    try:
        with open(pdf_path, "rb") as handle:
            if handle.read(5) == b"%PDF-":
                return None
    except OSError:
        pass
    pdf_path.unlink(missing_ok=True)
    return "Chromium did not produce a PDF, so only the HTML report was made."


# ---------------------------------------------------------------- build


def module_unknowns(brief: dict[str, Any] | None, modules: dict[str, dict[str, Any] | None]) -> list[str]:
    lines: list[str] = []
    report = (brief or {}).get("report") if isinstance(brief, dict) else None
    if isinstance(report, dict) and as_list(report.get("unknowns")):
        lines += [u for u in report["unknowns"] if isinstance(u, str)]
    else:
        for module in modules.values():
            if isinstance(module, dict):
                lines += [u for u in as_list(module.get("unknowns")) if isinstance(u, str)]
    if modules["speed"] is None:
        lines.append("Speed: not measured this run (no performance step).")
    if modules["gaps"] is None:
        lines.append("Content gaps: not checked this run (no focus keywords or no search step).")
    if modules["opportunities"] is None:
        lines.append("Search Console: no click data this run, so there are no click estimates.")
    seen, out = set(), []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def error_output(message: str, site: str = "", score: int | None = None) -> dict[str, Any]:
    return {"outcome": "error", "site": site, "score": score, "files": [], "sections": [], "sentence": message, "unknowns": [message]}


def build(inputs: dict[str, Any], out_dir: Path) -> tuple[dict[str, Any], int]:
    brief = unwrap(inputs.get("brief"))
    modules = {name: unwrap(inputs.get(name)) for name in ("audit", "speed", "gaps", "opportunities")}
    audit = modules["audit"]
    if audit is not None and audit.get("outcome") == "error":
        audit = None
    if brief is None and audit is None:
        return error_output("Nothing to render: neither the brief nor the page audit arrived."), 1
    site = str((brief or {}).get("site") or (audit or {}).get("site") or "")
    score = as_int((brief or {}).get("score"))
    if score is None:
        score = as_int((audit or {}).get("overall_score"))
    report = (brief or {}).get("report") if isinstance((brief or {}).get("report"), dict) else {}
    verdict = report.get("summary") or (audit or {}).get("sentence") or ""
    now = inputs.get("now") if isinstance(inputs.get("now"), str) else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    actions = [a for a in as_list((brief or {}).get("actions")) if isinstance(a, dict)]

    parts = [
        ("summary", hero(site, score, verdict, audit, modules["speed"], modules["opportunities"], now[:10])),
        ("changes", changes_section(actions)),
        ("scorecard", scorecard_section(audit)),
        ("speed", speed_section(modules["speed"])),
        ("content_gaps", gaps_section(modules["gaps"])),
        ("opportunities", opportunities_section(modules["opportunities"])),
        ("fixes", fixes_section(actions)),
        ("findings", findings_section([audit, modules["speed"], modules["gaps"]])),
        ("unknowns", unknowns_section(module_unknowns(brief, modules))),
    ]
    sections = [name for name, body in parts if body]
    footer = "<footer>Every change is a draft: check the facts before you publish. Made by the SEO Optimizer from public pages and connected data.</footer>"
    title = f"SEO Optimizer · {host_of(site)} — {score if score is not None else '–'}/100"
    document = page_html(title, "".join(body for _, body in parts) + footer)

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        html_path = out_dir / f"{BASENAME}.html"
        html_path.write_text(document, encoding="utf-8")
    except OSError as exc:
        return error_output(f"The report could not be written ({type(exc).__name__}).", site, score), 1
    pdf_path = out_dir / f"{BASENAME}.pdf"
    problem = print_pdf(html_path, pdf_path)
    files = []
    if problem is None:
        files.append({"path": str(pdf_path), "kind": "pdf", "bytes": pdf_path.stat().st_size})
    files.append({"path": str(html_path), "kind": "html", "bytes": html_path.stat().st_size})
    unknowns = [problem] if problem else []
    names = " and ".join(Path(f["path"]).name for f in files)
    sentence = f"Report for {host_of(site)}: {score if score is not None else '–'}/100, {len(actions)} changes, {len(sections)} sections, saved as {names}."
    output = {"outcome": "partial" if problem else "ok", "site": site, "score": score, "files": files, "sections": sections, "sentence": sentence, "unknowns": unknowns}
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
# (input, earlier module, key in its output; None for the whole output).
EARLIER_STEPS = (
    ("audit", "website-seo-page-audit", None),
    ("speed", "website-seo-performance", None),
    ("gaps", "website-seo-content-gap", None),
    ("opportunities", "website-seo-opportunities", None),
)


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
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    try:
        inputs = read_inputs(args.input)
        if isinstance(inputs, dict):
            inputs = from_earlier_steps(inputs, args.steps_dir, (unwrap(inputs.get("brief")) or {}).get("site"))
        output, code = build(inputs if isinstance(inputs, dict) else {}, Path(args.out_dir))
    except (OSError, ValueError) as exc:
        print(f"could not read the inputs: {type(exc).__name__}", file=sys.stderr)
        output, code = error_output("The inputs could not be read."), 1
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
