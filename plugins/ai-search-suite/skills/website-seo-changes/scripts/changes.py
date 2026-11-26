#!/usr/bin/env python3
"""Say what changed in a site's SEO since the last run, deterministically.

Run inside JoyStream's sandbox by the website-seo-changes skill:

    python3 scripts/changes.py --input /tmp/website-seo-changes.input.json

Inputs come from `--input FILE`, else INPUT_JSON or the file named by INPUT_JSON_PATH. The
page audit, speed and content-gap outputs are read from the files those steps saved in this
sandbox (`--steps-dir`, default /tmp) when they are not given. The previous run's snapshot
comes in `previous`, as the snapshot itself, its JSON text, or the Google Sheets value range
it was saved in.

Standard library only; fetches nothing. Prints exactly one JSON object on stdout, valid
against assets/output.schema.json, and writes the same object to /tmp/website-seo-changes.json.
When there is no usable audit it still prints a valid object and exits 1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

DEFAULT_OUT = "/tmp/website-seo-changes.json"
SKILL = "website-seo-changes"
VERSION = "1.0.0"
SNAPSHOT_KIND = "seo-watch-snapshot"
SHEET_HEADER = ["taken_at", "site", "score", "snapshot"]
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
URGENT = ("critical", "high")
RATING_ORDER = {"good": 0, "needs_improvement": 1, "poor": 2}
RATING_WORDS = {"good": "good", "needs_improvement": "needs improvement", "poor": "poor"}
VITALS = (("lcp", "LCP"), ("cls", "CLS"), ("tbt", "TBT"))
TOP_HOSTS = 5
MAX_ITEMS = 8
TITLE_CHARS = 90


# ---------------------------------------------------------------- reading inputs


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
            return value
        if value.get("ok") is False:
            return None
        if isinstance(value.get("step"), dict):
            value = value["step"]
            continue
        if "data" in value and len(value) <= 4 and set(value) <= {"ok", "data", "status", "error"}:
            value = value["data"]
            continue
        if len(value) == 1:
            only = next(iter(value.values()))
            if isinstance(only, dict) and ("site" in only or "outcome" in only):
                value = only
                continue
        return value
    return value


def find_snapshot(value: Any) -> dict[str, Any] | None:
    """The newest snapshot in `previous`: itself, its JSON text, or a cell of a value range."""
    found: list[dict[str, Any]] = []

    def visit(item: Any, depth: int) -> None:
        if depth > 6:
            return
        if isinstance(item, str):
            text = item.strip()
            if text.startswith("{") and SNAPSHOT_KIND in text:
                try:
                    visit(json.loads(text), depth + 1)
                except ValueError:
                    return
            return
        if isinstance(item, dict):
            if item.get("kind") == SNAPSHOT_KIND:
                found.append(item)
                return
            for child in item.values():
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)

    visit(value, 0)
    if not found:
        return None
    return max(found, key=lambda s: str(s.get("taken_at") or ""))


def host_of(url: Any) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    text = url.strip()
    netloc = urlsplit(text if "//" in text else f"https://{text}").netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def path_of(url: Any) -> str:
    if not isinstance(url, str):
        return ""
    parts = urlsplit(url.strip() if "//" in url else f"https://{url.strip()}")
    return parts.path or "/"


def moment_of(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def day_of(value: Any) -> str:
    moment = moment_of(value)
    return moment.date().isoformat() if moment else ""


def short_title(text: Any) -> str:
    title = " ".join(str(text or "").split())
    return title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1].rstrip() + "…"


# ---------------------------------------------------------------- this run's state


def current_findings(audit: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in audit.get("findings") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        severity = item.get("severity") if item.get("severity") in SEVERITY_ORDER else "low"
        pages = item.get("pages") if isinstance(item.get("pages"), list) else []
        out[item["id"]] = {"severity": severity, "title": short_title(item.get("title") or item["id"]), "pages": len(pages)}
    return out


def current_ranks(gaps: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(gaps, dict):
        return out
    for entry in gaps.get("keywords") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("keyword"), str):
            continue
        if entry.get("error") or not isinstance(entry.get("competitors"), list):
            continue
        rank = entry.get("our_rank") if isinstance(entry.get("our_rank"), int) else None
        hosts = []
        for competitor in entry["competitors"]:
            host = host_of(competitor.get("url") if isinstance(competitor, dict) else None)
            if host and host not in hosts:
                hosts.append(host)
        out[entry["keyword"]] = {"rank": rank, "top": hosts[:TOP_HOSTS]}
    return out


def current_speed(speed: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(speed, dict):
        return out
    for page in speed.get("pages") or []:
        if not isinstance(page, dict) or page.get("status") not in (None, "ok") or not isinstance(page.get("url"), str):
            continue
        ratings = page.get("ratings") if isinstance(page.get("ratings"), dict) else {}
        entry = {key: ratings.get(key) for key, _ in VITALS if ratings.get(key) in RATING_ORDER}
        if entry:
            out[page["url"]] = entry
    return out


def usable(module: Any) -> dict[str, Any] | None:
    module = unwrap(module)
    if not isinstance(module, dict) or module.get("outcome") == "error":
        return None
    return module


# ---------------------------------------------------------------- comparing


def compare(prev: dict[str, Any], findings: dict[str, dict[str, Any]], ranks: dict[str, dict[str, Any]], speed: dict[str, dict[str, Any]], score: Any, today: str, escalate_after: int) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    before = prev.get("findings") if isinstance(prev.get("findings"), dict) else {}
    remembered: dict[str, dict[str, Any]] = {}
    new, still_open, fixed = [], [], []
    for fid, item in findings.items():
        old = before.get(fid) if isinstance(before.get(fid), dict) else None
        since = (old or {}).get("since") or today
        seen = int((old or {}).get("seen") or 0) + 1
        remembered[fid] = {"s": item["severity"], "t": item["title"], "since": since, "seen": seen}
        row = {"id": fid, "severity": item["severity"], "title": item["title"], "pages": item["pages"]}
        if old is None:
            new.append(row)
            continue
        age = days_between(since, today)
        row.update({"since": since, "age_days": age, "checks": seen, "escalated": item["severity"] in URGENT and seen >= escalate_after})
        if old.get("s") in SEVERITY_ORDER and old.get("s") != item["severity"]:
            row["severity_before"] = old["s"]
        still_open.append(row)
    for fid, old in before.items():
        if fid not in findings and isinstance(old, dict):
            fixed.append({"id": fid, "severity": old.get("s") if old.get("s") in SEVERITY_ORDER else "low", "title": short_title(old.get("t") or fid)})
    order = lambda row: (SEVERITY_ORDER.get(row["severity"], 9), row["id"])  # noqa: E731
    new.sort(key=order)
    fixed.sort(key=order)
    still_open.sort(key=lambda row: (not row["escalated"], SEVERITY_ORDER.get(row["severity"], 9), -row["age_days"], row["id"]))

    rank_moves, new_competitors = [], []
    old_ranks = prev.get("ranks") if isinstance(prev.get("ranks"), dict) else {}
    for keyword, now_entry in ranks.items():
        old = old_ranks.get(keyword) if isinstance(old_ranks.get(keyword), dict) else None
        if old is None:
            continue
        if old.get("rank") != now_entry["rank"]:
            rank_moves.append({"keyword": keyword, "before": old.get("rank"), "after": now_entry["rank"], "direction": direction(old.get("rank"), now_entry["rank"])})
        old_hosts = set(old.get("top") or [])
        for host in now_entry["top"]:
            if old_hosts and host not in old_hosts:
                new_competitors.append({"keyword": keyword, "host": host})

    speed_moves = []
    old_speed = prev.get("speed") if isinstance(prev.get("speed"), dict) else {}
    for url, ratings in speed.items():
        old = old_speed.get(url) if isinstance(old_speed.get(url), dict) else None
        if old is None:
            continue
        for key, label in VITALS:
            a, b = old.get(key), ratings.get(key)
            if a in RATING_ORDER and b in RATING_ORDER and a != b:
                speed_moves.append({"url": url, "metric": label, "before": a, "after": b, "direction": "better" if RATING_ORDER[b] < RATING_ORDER[a] else "worse"})

    prev_score = prev.get("score") if isinstance(prev.get("score"), int) else None
    delta = score - prev_score if isinstance(score, int) and prev_score is not None else None
    changes = {
        "score": {"before": prev_score, "after": score if isinstance(score, int) else None, "delta": delta},
        "new": new,
        "fixed": fixed,
        "still_open": still_open,
        "ranks": rank_moves,
        "new_competitors": new_competitors,
        "speed": speed_moves,
    }
    return changes, remembered


def direction(before: Any, after: Any) -> str:
    if before is None:
        return "entered"
    if after is None:
        return "dropped_out"
    return "up" if after < before else "down"


def days_between(start: str, end: str) -> int:
    try:
        return max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days)
    except ValueError:
        return 0


# ---------------------------------------------------------------- the card


def rank_text(rank: Any) -> str:
    return f"#{rank}" if isinstance(rank, int) else "not in the top results"


def pages_text(row: dict[str, Any]) -> str:
    count = row.get("pages") or 0
    return f" ({count} page{'s' if count != 1 else ''})" if count else ""


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def item(text: str, citations: list[str] | None = None) -> dict[str, Any]:
    return {"text": text[:400], "citations": citations or []}


def capped(rows: list[dict[str, Any]], render) -> list[dict[str, Any]]:
    shown = [item(render(row)) for row in rows[:MAX_ITEMS]]
    if len(rows) > MAX_ITEMS:
        shown.append(item(f"{len(rows) - MAX_ITEMS} more in the report."))
    return shown


def link_section(report_url: str | None, report_file: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not report_url:
        if report_file:
            return [{"heading": "Full report", "items": [item(f"{report_file} in this run's files: every finding and copy-ready fix.")]}], []
        return [], []
    text = f"{report_file} in this run's files: every finding and copy-ready fix. [Open the report]({report_url})" if report_file else f"Every finding and copy-ready fix: [open the run]({report_url})"
    return [{"heading": "Full report", "items": [item(text, ["S0"])]}], [{"id": "S0", "title": "Full report and run details", "url": report_url}]


def first_run_report(host: str, score: Any, findings: dict[str, dict[str, Any]], ranks: dict[str, dict[str, Any]], link: tuple[list, list]) -> dict[str, Any]:
    urgent = sorted((dict(id=fid, **row) for fid, row in findings.items() if row["severity"] in URGENT), key=lambda r: (SEVERITY_ORDER[r["severity"]], r["id"]))
    sections = []
    if urgent:
        sections.append({"heading": "Fix these first", "items": capped(urgent, lambda r: f"{r['severity'].capitalize()}: {r['title']}{pages_text(r)}")})
    if ranks:
        sections.append({"heading": "Rankings now", "items": [item(f"\"{kw}\": {rank_text(e['rank'])}") for kw, e in ranks.items()][:MAX_ITEMS]})
    sections += link[0]
    score_text = f"{score}/100" if isinstance(score, int) else "not scored"
    return {
        "title": f"SEO Watch · {host} — {score_text} (first check)",
        "summary": f"First check: saved as the baseline, so the next run reports only what changed. {len(urgent)} high-priority issue{'s' if len(urgent) != 1 else ''} open, {len(findings)} in all.",
        "severity": "attention" if urgent else "info",
        "sections": sections,
        "sources": link[1],
    }


def changes_report(host: str, prev_day: str, changes: dict[str, Any], link: tuple[list, list]) -> tuple[dict[str, Any], bool]:
    new, fixed, still = changes["new"], changes["fixed"], changes["still_open"]
    escalated = [row for row in still if row["escalated"]]
    worse_speed = [row for row in changes["speed"] if row["direction"] == "worse"]
    rank_drops = [row for row in changes["ranks"] if row["direction"] in ("down", "dropped_out")]
    quiet = not (new or fixed or changes["ranks"] or changes["new_competitors"] or changes["speed"] or changes["score"]["delta"])

    sections = []
    if new:
        sections.append({"heading": f"New since {prev_day}", "items": capped(new, lambda r: f"{r['severity'].capitalize()}: {r['title']}{pages_text(r)}")})
    if escalated:
        sections.append({"heading": "Act now: still open", "items": capped(escalated, lambda r: f"{r['severity'].capitalize()}, open {r['age_days']} days ({ordinal(r['checks'])} check): {r['title']}{pages_text(r)}")})
    if fixed:
        sections.append({"heading": "Fixed", "items": capped(fixed, lambda r: f"{r['title']}")})
    rank_items = [item(f"\"{r['keyword']}\": {rank_text(r['before'])} → {rank_text(r['after'])}") for r in changes["ranks"]]
    rank_items += [item(f"New in the top {TOP_HOSTS} for \"{r['keyword']}\": {r['host']}") for r in changes["new_competitors"]]
    if rank_items:
        sections.append({"heading": "Rankings", "items": rank_items[:MAX_ITEMS]})
    if changes["speed"]:
        sections.append({"heading": "Speed", "items": capped(changes["speed"], lambda r: f"{path_of(r['url'])} {r['metric']}: {RATING_WORDS[r['before']]} → {RATING_WORDS[r['after']]}")})
    rest = [row for row in still if not row["escalated"]]
    if rest:
        counts = {}
        for row in rest:
            counts[row["severity"]] = counts.get(row["severity"], 0) + 1
        parts = ", ".join(f"{n} {sev}" for sev, n in sorted(counts.items(), key=lambda kv: SEVERITY_ORDER[kv[0]]))
        sections.append({"heading": "Still open", "items": [item(f"{len(rest)} more open issue{'s' if len(rest) != 1 else ''} ({parts}); see the report.")]})
    sections += link[0]

    score = changes["score"]
    score_text = f"{score['after']}/100" if isinstance(score["after"], int) else "not scored"
    if isinstance(score["delta"], int) and score["delta"]:
        score_text += f" ({'▲ +' if score['delta'] > 0 else '▼ −'}{abs(score['delta'])})"
    urgent_open = [row for row in still if row["severity"] in URGENT] + [row for row in new if row["severity"] in URGENT]
    if quiet:
        oldest = max((row["age_days"] for row in still if row["severity"] in URGENT), default=0)
        summary = f"No change since {prev_day}."
        if urgent_open:
            summary += f" {len(urgent_open)} high-priority issue{'s' if len(urgent_open) != 1 else ''} still open, the oldest for {oldest} days."
    else:
        parts = [f"{len(new)} new", f"{len(fixed)} fixed", f"{len(still)} still open"]
        if escalated:
            parts[-1] += f" ({len(escalated)} high-priority, open since an earlier check)"
        summary = f"Since {prev_day}: " + ", ".join(parts) + "."
        ups = [row for row in changes["ranks"] if row["direction"] in ("up", "entered")]
        if ups or rank_drops:
            summary += f" Rankings: {len(ups)} up, {len(rank_drops)} down."
    if any(row["severity"] in URGENT for row in new) or escalated:
        severity = "action_needed"
    elif new or rank_drops or worse_speed or changes["new_competitors"]:
        severity = "attention"
    else:
        severity = "info"
    return {"title": f"SEO Watch · {host} — {score_text}", "summary": summary[:600], "severity": severity, "sections": sections, "sources": link[1]}, quiet


# ---------------------------------------------------------------- build


def build(inputs: dict[str, Any]) -> tuple[dict[str, Any], int]:
    now = inputs.get("now") if moment_of(inputs.get("now")) else None
    taken_at = now or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    today = day_of(taken_at)
    report_url = inputs.get("report_url") if isinstance(inputs.get("report_url"), str) and inputs["report_url"].startswith("https://") else None
    report_file = inputs.get("report_file") if isinstance(inputs.get("report_file"), str) and inputs["report_file"].strip() else None
    escalate_after = inputs.get("escalate_after_checks")
    escalate_after = max(1, min(escalate_after, 10)) if isinstance(escalate_after, int) and not isinstance(escalate_after, bool) else 2
    unknowns: list[str] = []

    site = inputs.get("site_url") if isinstance(inputs.get("site_url"), str) else ""
    audit = usable(inputs.get("audit"))
    site = site or (audit or {}).get("site") or ""
    host = host_of(site)
    prev = find_snapshot(inputs.get("previous"))
    if prev is not None and host and host_of(prev.get("site")) != host:
        unknowns.append(f"The saved snapshot is for {host_of(prev.get('site')) or 'another site'}, not {host}, so this run starts a new baseline.")
        prev = None
    if prev is None and inputs.get("previous") not in (None, "", [], {}) and not unknowns:
        unknowns.append("The previous snapshot could not be read, so this run starts a new baseline.")

    if not host or audit is None:
        message = "The page audit did not run or failed, so nothing was compared and the saved snapshot is kept."
        return {
            "outcome": "error",
            "site": site,
            "taken_at": taken_at,
            "previous_at": (prev or {}).get("taken_at"),
            "score": None,
            "quiet": False,
            "changes": None,
            "snapshot": prev,
            "sheet_values": [SHEET_HEADER] + ([[prev.get("taken_at") or "", prev.get("site") or "", prev.get("score") if isinstance(prev.get("score"), int) else "", json.dumps(prev, ensure_ascii=False, separators=(",", ":"))]] if prev else []),
            "report": finish({"title": f"SEO Watch · {host or 'site'} — not checked", "summary": message, "severity": "attention", "sections": [], "sources": []}, unknowns + [message], now),
            "sentence": message,
            "unknowns": unknowns + [message],
        }, 1

    score = audit.get("overall_score") if isinstance(audit.get("overall_score"), int) else None
    findings = current_findings(audit)
    speed_module = usable(inputs.get("speed"))
    gaps_module = usable(inputs.get("gaps"))
    speed = current_speed(speed_module)
    ranks = current_ranks(gaps_module)
    if not speed:
        unknowns.append("Speed was not measured this run; the last measured ratings are kept for the next comparison.")
    if not ranks:
        unknowns.append("Rankings were not checked this run; the last ones are kept for the next comparison.")
    link = link_section(report_url, report_file)

    if prev is None:
        remembered = {fid: {"s": row["severity"], "t": row["title"], "since": today, "seen": 1} for fid, row in findings.items()}
        report = first_run_report(host, score, findings, ranks, link)
        changes, quiet, outcome = None, False, "first_run"
        sentence = f"First SEO Watch check of {host}: {score if score is not None else '?'}/100, {len(findings)} open issues saved as the baseline."
    else:
        changes, remembered = compare(prev, findings, ranks, speed, score, today, escalate_after)
        report, quiet = changes_report(host, day_of(prev.get("taken_at")) or "the last check", changes, link)
        outcome = "ok"
        sentence = f"SEO Watch for {host}: {len(changes['new'])} new, {len(changes['fixed'])} fixed, {len(changes['still_open'])} still open" + (" (no change)." if quiet else ".")

    snapshot = {
        "kind": SNAPSHOT_KIND,
        "v": 1,
        "site": f"https://{host}",
        "taken_at": taken_at,
        "score": score,
        "findings": remembered,
        "ranks": ranks or ((prev or {}).get("ranks") or {}),
        "speed": speed or ((prev or {}).get("speed") or {}),
    }
    row = [taken_at, snapshot["site"], score if score is not None else "", json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))]
    return {
        "outcome": outcome,
        "site": snapshot["site"],
        "taken_at": taken_at,
        "previous_at": (prev or {}).get("taken_at"),
        "score": score,
        "quiet": quiet,
        "changes": changes,
        "snapshot": snapshot,
        "sheet_values": [SHEET_HEADER, row],
        "report": finish(report, unknowns, now),
        "sentence": sentence,
        "unknowns": unknowns,
    }, 0


def finish(report: dict[str, Any], unknowns: list[str], now: str | None) -> dict[str, Any]:
    report["unknowns"] = unknowns[:20]
    if now:
        report["meta"] = {"skill": SKILL, "skill_version": VERSION, "generated_at": now}
    return report


# ---------------------------------------------------------------- earlier steps


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
EARLIER_STEPS = (
    ("audit", "website-seo-page-audit", None),
    ("speed", "website-seo-performance", None),
    ("gaps", "website-seo-content-gap", None),
)


def from_earlier_steps(inputs: dict[str, Any], steps_dir: str, site: Any) -> dict[str, Any]:
    """Fill each input left out from the output an earlier module saved in this sandbox.

    A saved output counts only for the same site, and only when it was written after the
    run started (the `now` input, when given): an older file belongs to another run.
    """
    moment = moment_of(inputs.get("now"))
    started = moment.timestamp() if moment else None
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


def same_site(saved: Any, site: Any) -> bool:
    """The same host, ignoring the scheme and www. With no site of our own, any matches."""
    ours = host_of(site)
    return not ours or host_of(saved) == ours


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
        output, code = build({})
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
