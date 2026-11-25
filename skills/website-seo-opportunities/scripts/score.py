#!/usr/bin/env python3
"""Find a site's best SEO opportunities in its Search Console rows, deterministically.

Run inside JoyStream's sandbox by the website-seo-opportunities skill:

    python3 scripts/score.py

Inputs come from INPUT_JSON or, when that is over 64 KiB, the file named by
INPUT_JSON_PATH. `--input FILE` reads them from a file instead (tests, local runs).

Standard library only. Prints exactly one JSON object on stdout, valid against
assets/output.schema.json, and writes the same object to
/tmp/website-seo-opportunities.json. On bad input it still prints a valid object and exits 1.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from typing import Any
from urllib.parse import urlsplit

DEFAULT_OUT = "/tmp/website-seo-opportunities.json"
ROW_LIMIT = 25000

# Fallback expected CTR by position, used only where the site has too few impressions at a
# position to measure its own. Deliberately conservative (below the usual no-AI-answer curves),
# so a site with little data is not told to expect clicks it will not get.
DEFAULT_CURVE = {1: 0.22, 2: 0.13, 3: 0.09, 4: 0.06, 5: 0.045, 6: 0.035, 7: 0.028, 8: 0.022, 9: 0.018, 10: 0.015, 11: 0.008}
CURVE_MIN_IMPRESSIONS = 500
CURVE_MIN_QUERIES = 5
CANNIBAL_LIFT = 3
CTR_GAP_RATIO = 0.7
EFFORT_POINTS = {"quick fix": 1.0, "project": 3.0}
PER_PAGE_CAP = 2
DECAY_MIN_PREVIOUS_CLICKS = 20
DECAY_MIN_DROP_SHARE = 0.3
DECAY_MIN_DROP_CLICKS = 10
RANKING_DECAY_POSITIONS = 1.0
CANNIBAL_MIN_SHARE = 0.1
_INFORMATIONAL = re.compile(
    r"^(what|how|why|when|where|who|which|is|are|can|does|do|should|guide|tutorial|example|examples|definition|meaning)\b",
    re.IGNORECASE,
)


def read_inputs(path: str | None) -> dict[str, Any]:
    if path:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    file_path = os.environ.get("INPUT_JSON_PATH")
    if file_path:
        with open(file_path, encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(os.environ.get("INPUT_JSON") or "{}")


def find_rows(value: Any) -> list[dict[str, Any]] | None:
    """The `rows` list of a Search Analytics response, however the step's output wraps it.

    A response with no rows at all (no data in the window) has no `rows` key; that reads as
    an empty list when the object is otherwise a successful response."""
    if isinstance(value, dict):
        if isinstance(value.get("rows"), list):
            return [row for row in value["rows"] if isinstance(row, dict)]
        if "responseAggregationType" in value:
            return []
        for child in value.values():
            found = find_rows(child)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = find_rows(child)
            if found is not None:
                return found
    return None


def number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def clean_page(url: str) -> str:
    return url.split("#", 1)[0]


def parse_rows(raw: Any) -> tuple[list[dict[str, Any]] | None, bool]:
    """(query × page rows, or None when unreadable; complete)."""
    rows = find_rows(raw)
    if rows is None:
        return None, False
    parsed = []
    for row in rows:
        keys = row.get("keys")
        if not isinstance(keys, list) or len(keys) < 2:
            continue
        query, page = str(keys[0]).strip(), clean_page(str(keys[1]).strip())
        if not query or not page:
            continue
        parsed.append(
            {
                "query": query,
                "page": page,
                "clicks": number(row.get("clicks")),
                "impressions": number(row.get("impressions")),
                "position": number(row.get("position")) or 0.0,
            }
        )
    return parsed, len(rows) < ROW_LIMIT


def brand_terms_of(value: Any, site_url: str) -> list[str]:
    parts = re.split(r"[\n,]+", value) if isinstance(value, str) else value if isinstance(value, list) else []
    terms = [re.sub(r"\s+", "", str(p)).lower() for p in parts if str(p).strip()]
    if not terms:
        host = (urlsplit(site_url if "//" in site_url else f"//{site_url}").hostname or "").lower()
        host = host.removeprefix("sc-domain:").removeprefix("www.")
        label = host.split(".")[0] if host else ""
        if len(label) >= 3:
            terms = [label]
    return terms


def is_branded(query: str, terms: list[str]) -> bool:
    squashed = re.sub(r"\s+", "", query).lower()
    return any(term in squashed for term in terms)


_MEASURED: set[int] = set()


def benchmark(b: int, what: str) -> str:
    """Who the expected CTR belongs to: the site's own pages, or the default curve."""
    return f"this site's {what}" if b in _MEASURED else f"the benchmark {what}"


def bucket(position: float) -> int:
    rounded = max(1, int(position + 0.5))
    return rounded if rounded <= 10 else 11


def build_curve(rows: list[dict[str, Any]]) -> tuple[dict[int, float], list[dict[str, Any]]]:
    _MEASURED.clear()
    clicks = dict.fromkeys(DEFAULT_CURVE, 0.0)
    impressions = dict.fromkeys(DEFAULT_CURVE, 0.0)
    queries: dict[int, set[str]] = {b: set() for b in DEFAULT_CURVE}
    for row in rows:
        if row["position"] <= 0 or row["position"] > 20.5:
            continue
        b = bucket(row["position"])
        clicks[b] += row["clicks"]
        impressions[b] += row["impressions"]
        queries[b].add(row["query"].lower())
    curve: dict[int, float] = {}
    table = []
    previous = 1.0
    for b in sorted(DEFAULT_CURVE):
        # One big query is not a curve: measuring a row against itself would hide its own gap.
        own = impressions[b] >= CURVE_MIN_IMPRESSIONS and len(queries[b]) >= CURVE_MIN_QUERIES
        value = clicks[b] / impressions[b] if own else DEFAULT_CURVE[b]
        value = min(value, previous)  # a lower position never expects more clicks
        previous = value
        curve[b] = value
        if own:
            _MEASURED.add(b)
        table.append(
            {
                "position": str(b) if b <= 10 else "11-20",
                "expected_ctr": round(value, 4),
                "source": "site" if own else "default",
                "impressions": int(impressions[b]),
            }
        )
    return curve, table


def weighted_position(rows: list[dict[str, Any]]) -> float | None:
    impressions = sum(r["impressions"] for r in rows)
    if impressions <= 0:
        return None
    return sum(r["position"] * r["impressions"] for r in rows) / impressions


def by_page(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    pages: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        pages.setdefault(row["page"], []).append(row)
    return pages


def fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def fmt_int(value: float) -> str:
    return f"{int(round(value)):,}"


def query_opportunities(
    rows: list[dict[str, Any]], curve: dict[int, float], terms: list[str], minimum: int, skip: set[str]
) -> list[dict[str, Any]]:
    found = []
    for row in rows:
        if row["query"].lower() in skip:
            continue
        if is_branded(row["query"], terms) or row["impressions"] < minimum or not 0 < row["position"] <= 20.5:
            continue
        ctr = row["clicks"] / row["impressions"]
        expected = curve[bucket(row["position"])]
        informational = bool(_INFORMATIONAL.match(row["query"]))
        base = {
            "query": row["query"],
            "page": row["page"],
            "clicks": int(row["clicks"]),
            "impressions": int(row["impressions"]),
            "ctr": round(ctr, 4),
            "position": round(row["position"], 1),
            "expected_ctr": round(expected, 4),
            "ai_overview_risk": informational,
            "competing_pages": [],
            "previous_clicks": None,
            "previous_position": None,
        }
        if row["position"] <= 10.5 and ctr < CTR_GAP_RATIO * expected:
            extra = row["impressions"] * expected - row["clicks"]
            why = (
                f'Ranks {row["position"]:.1f} for "{row["query"]}" with {fmt_int(row["impressions"])} impressions '
                f"but a {fmt_pct(ctr)} CTR; {benchmark(bucket(row['position']), 'CTR at that position')} is {fmt_pct(expected)}."
            )
            if informational:
                why += " Informational query: an AI answer on the results page may take some of these clicks."
            found.append({**base, "type": "ctr_gap", "target_position": bucket(row["position"]), "target_ctr": round(expected, 4), "extra_clicks": extra, "effort": "quick fix", "why": why})
        elif bucket(row["position"]) >= 4:
            target = 3 if row["position"] <= 10.5 else 8
            target_ctr = curve[target]
            extra = row["impressions"] * target_ctr - row["clicks"]
            why = (
                f'Ranks {row["position"]:.1f} for "{row["query"]}" ({fmt_int(row["impressions"])} impressions); '
                f"reaching position {target} at {benchmark(target, 'CTR there')} ({fmt_pct(target_ctr)}) adds about {fmt_int(max(extra, 0))} clicks."
            )
            found.append({**base, "type": "striking_distance", "target_position": target, "target_ctr": round(target_ctr, 4), "extra_clicks": extra, "effort": "project", "why": why})
    return found


def cannibalization(rows: list[dict[str, Any]], curve: dict[int, float], terms: list[str], minimum: int) -> list[dict[str, Any]]:
    queries: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not is_branded(row["query"], terms):
            queries.setdefault(row["query"], []).append(row)
    found = []
    for query, group in queries.items():
        total = sum(r["impressions"] for r in group)
        if total < minimum:
            continue
        splitting = [r for r in group if r["clicks"] >= 1 and r["impressions"] >= CANNIBAL_MIN_SHARE * total]
        if len(splitting) < 2:
            continue
        splitting.sort(key=lambda r: (-r["clicks"], r["position"], r["page"]))
        keep = splitting[0]
        clicks = sum(r["clicks"] for r in group)
        best = min(r["position"] for r in splitting)
        if best > 20.5:
            continue
        expected = curve[bucket(best)]
        target = max(1, bucket(best) - CANNIBAL_LIFT)
        target_ctr = curve[target]
        extra = total * target_ctr - clicks
        others = ", ".join(f'{urlsplit(r["page"]).path or "/"} (position {r["position"]:.1f})' for r in splitting)
        found.append(
            {
                "type": "cannibalization",
                "query": query,
                "page": keep["page"],
                "clicks": int(clicks),
                "impressions": int(total),
                "ctr": round(clicks / total, 4) if total else 0.0,
                "position": round(best, 1),
                "expected_ctr": round(expected, 4),
                "target_position": target,
                "target_ctr": round(target_ctr, 4),
                "extra_clicks": extra,
                "effort": "project",
                "ai_overview_risk": bool(_INFORMATIONAL.match(query)),
                "competing_pages": [r["page"] for r in splitting[1:]],
                "previous_clicks": None,
                "previous_position": None,
                "why": (
                    f'{len(splitting)} pages split clicks for "{query}": {others}. Consolidating on '
                    f'{urlsplit(keep["page"]).path or "/"} and reaching position {target} adds about {fmt_int(max(extra, 0))} clicks.'
                ),
            }
        )
    return found


def decay(current: list[dict[str, Any]], previous: list[dict[str, Any]], year_ago: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now_pages, before_pages = by_page(current), by_page(previous)
    year_pages = by_page(year_ago) if year_ago is not None else {}
    ranking, demand = [], []
    for page, before in before_pages.items():
        before_clicks = sum(r["clicks"] for r in before)
        after = now_pages.get(page, [])
        after_clicks = sum(r["clicks"] for r in after)
        drop = before_clicks - after_clicks
        if before_clicks < DECAY_MIN_PREVIOUS_CLICKS or drop < max(DECAY_MIN_DROP_CLICKS, DECAY_MIN_DROP_SHARE * before_clicks):
            continue
        before_pos, after_pos = weighted_position(before), weighted_position(after)
        after_impressions = sum(r["impressions"] for r in after)
        year_clicks = sum(r["clicks"] for r in year_pages.get(page, [])) if year_ago is not None else None
        slipped = before_pos is not None and (after_pos is None or after_pos - before_pos >= RANKING_DECAY_POSITIONS)
        top = max(before, key=lambda r: (r["clicks"], r["impressions"], r["query"]))
        entry = {
            "page": page,
            "clicks": int(after_clicks),
            "previous_clicks": int(before_clicks),
            "year_ago_clicks": int(year_clicks) if year_clicks is not None else None,
            "position": round(after_pos, 1) if after_pos is not None else None,
            "previous_position": round(before_pos, 1) if before_pos is not None else None,
            "top_query": top["query"],
        }
        if slipped:
            where = f"slipped from {before_pos:.1f} to {after_pos:.1f}" if after_pos is not None else f"dropped out (was {before_pos:.1f})"
            ranking.append(
                {
                    "type": "content_decay",
                    "query": top["query"],
                    "page": page,
                    "clicks": int(after_clicks),
                    "impressions": int(after_impressions),
                    "ctr": round(after_clicks / after_impressions, 4) if after_impressions else 0.0,
                    "position": round(after_pos, 1) if after_pos is not None else round(before_pos, 1),
                    "expected_ctr": None,
                    "target_position": max(1, int(before_pos + 0.5)),
                    "target_ctr": None,
                    "extra_clicks": float(drop),
                    "effort": "project",
                    "ai_overview_risk": False,
                    "competing_pages": [],
                    "previous_clicks": int(before_clicks),
                    "previous_position": round(before_pos, 1),
                    "why": f"Clicks fell from {fmt_int(before_clicks)} to {fmt_int(after_clicks)} in 28 days while average position {where}.",
                }
            )
        else:
            entry["why"] = (
                f"Clicks fell from {fmt_int(before_clicks)} to {fmt_int(after_clicks)} but position held"
                + (f" ({before_pos:.1f} → {after_pos:.1f})" if before_pos is not None and after_pos is not None else "")
                + ": fewer people searched, so a rewrite will not bring them back."
            )
            demand.append(entry)
    demand.sort(key=lambda e: (-(e["previous_clicks"] - e["clicks"]), e["page"]))
    return ranking, demand


def top_queries(rows: list[dict[str, Any]], page: str) -> list[dict[str, Any]]:
    mine = sorted((r for r in rows if r["page"] == page), key=lambda r: (-r["impressions"], -r["clicks"], r["query"]))[:5]
    return [{"query": r["query"], "clicks": int(r["clicks"]), "impressions": int(r["impressions"]), "position": round(r["position"], 1)} for r in mine]


def error_output(message: str) -> dict[str, Any]:
    return {
        "outcome": "error",
        "site": "",
        "rows": {"current": None, "previous": None, "year_ago": None},
        "totals": {"clicks": None, "impressions": None, "ctr": None, "position": None, "previous_clicks": None, "clicks_change_pct": None},
        "ctr_curve": [],
        "opportunities": [],
        "demand_decay": [],
        "target_urls": [],
        "target_queries": [],
        "extra_clicks_total": 0,
        "complete": False,
        "sentence": "",
        "unknowns": [message],
    }


def build(inputs: dict[str, Any]) -> tuple[dict[str, Any], int]:
    site = str(inputs.get("site_url") or "").strip()
    current, current_complete = parse_rows(inputs.get("current"))
    if current is None:
        output = error_output("The Search Console rows for the current period could not be read, so nothing was scored.")
        output["site"] = site
        return output, 1
    previous_given = inputs.get("previous") is not None
    previous, previous_complete = parse_rows(inputs.get("previous")) if previous_given else (None, True)
    year_given = inputs.get("year_ago") is not None
    year_ago, _ = parse_rows(inputs.get("year_ago")) if year_given else (None, True)

    top_n = inputs.get("top_n") if isinstance(inputs.get("top_n"), int) else 10
    top_n = max(1, min(top_n, 25))
    minimum = inputs.get("min_impressions") if isinstance(inputs.get("min_impressions"), int) else 50
    minimum = max(1, minimum)
    terms = brand_terms_of(inputs.get("brand_terms"), site)

    unbranded = [r for r in current if not is_branded(r["query"], terms)]
    curve, curve_table = build_curve(unbranded)

    split = cannibalization(current, curve, terms, minimum)
    split_queries = {item["query"].lower() for item in split}
    candidates = query_opportunities(current, curve, terms, minimum, split_queries) + split
    demand: list[dict[str, Any]] = []
    if previous is not None:
        ranking, demand = decay(current, previous, year_ago)
        candidates += ranking

    best_per_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for item in candidates:
        item["extra_clicks"] = max(0, int(round(item["extra_clicks"])))
        item["priority"] = round(item["extra_clicks"] / EFFORT_POINTS[item["effort"]], 2)
        key = (item["page"], item["query"].lower())
        held = best_per_pair.get(key)
        if held is None or (item["priority"], item["extra_clicks"]) > (held["priority"], held["extra_clicks"]):
            best_per_pair[key] = item
    ordered = sorted(
        (i for i in best_per_pair.values() if i["extra_clicks"] > 0),
        key=lambda i: (-i["priority"], -i["extra_clicks"], -i["impressions"], i["query"].lower(), i["page"]),
    )
    chosen, per_page = [], {}
    for item in ordered:
        if per_page.get(item["page"], 0) >= PER_PAGE_CAP:
            continue
        per_page[item["page"]] = per_page.get(item["page"], 0) + 1
        chosen.append(item)
        if len(chosen) == top_n:
            break
    for rank, item in enumerate(chosen, start=1):
        item["rank"] = rank
        item["page_top_queries"] = top_queries(current, item["page"])

    clicks = sum(r["clicks"] for r in current)
    impressions = sum(r["impressions"] for r in current)
    previous_clicks = sum(r["clicks"] for r in previous) if previous is not None else None
    position = weighted_position(current)
    extra_total = sum(i["extra_clicks"] for i in chosen)

    unknowns = [
        "Search Console hides rare (anonymized) queries, so these totals are lower than the property's own totals.",
        "Index coverage was not checked: these rows only include pages that already appear in search.",
    ]
    if not current_complete:
        unknowns.append(f"The current period hit the {ROW_LIMIT:,}-row limit, so lower-traffic rows were not read.")
    if not previous_given:
        unknowns.append("No previous period was given, so content decay was not checked.")
    elif previous is None:
        unknowns.append("The previous period's rows could not be read, so content decay was not checked.")
    elif not previous_complete:
        unknowns.append(f"The previous period hit the {ROW_LIMIT:,}-row limit, so decay may be missed on smaller pages.")
    if year_given and year_ago is None:
        unknowns.append("The year-ago rows could not be read, so year-over-year clicks are not shown.")
    defaults = [c["position"] for c in curve_table if c["source"] == "default"]
    if defaults:
        unknowns.append(
            "Too few impressions to measure this site's own CTR at position "
            + ", ".join(defaults)
            + f" (under {CURVE_MIN_IMPRESSIONS} each); a conservative default curve was used there."
        )
    if not terms:
        unknowns.append("No brand terms were given or derived, so branded queries were not excluded.")

    if chosen:
        sentence = (
            f"{len(chosen)} {'opportunity' if len(chosen) == 1 else 'opportunities'} worth about "
            f"{fmt_int(extra_total)} extra clicks per 28 days, from {fmt_int(clicks)} clicks today."
        )
    elif current:
        sentence = "No opportunity cleared the thresholds this period."
    else:
        sentence = "Search Console returned no rows for this period."

    ok = previous_given and previous is not None and current_complete and (not year_given or year_ago is not None)
    output = {
        "outcome": "ok" if ok else "partial",
        "site": site,
        "rows": {
            "current": len(current),
            "previous": len(previous) if previous is not None else None,
            "year_ago": len(year_ago) if year_ago is not None else None,
        },
        "totals": {
            "clicks": int(clicks),
            "impressions": int(impressions),
            "ctr": round(clicks / impressions, 4) if impressions else None,
            "position": round(position, 1) if position is not None else None,
            "previous_clicks": int(previous_clicks) if previous_clicks is not None else None,
            "clicks_change_pct": round((clicks - previous_clicks) / previous_clicks * 100, 1) if previous_clicks else None,
        },
        "ctr_curve": curve_table,
        "opportunities": chosen,
        "demand_decay": demand[:5],
        "target_urls": list(dict.fromkeys(i["page"] for i in chosen)),
        "target_queries": list(dict.fromkeys(i["query"] for i in chosen))[:10],
        "extra_clicks_total": int(extra_total),
        "complete": bool(current_complete and (previous is None or previous_complete)),
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
