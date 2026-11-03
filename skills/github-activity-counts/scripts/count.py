#!/usr/bin/env python3
"""Count merged and opened pull requests per repository, deterministically.

Run inside JoyStream's sandbox by the github-activity-counts skill:

    python3 scripts/count.py

Inputs come from INPUT_JSON or, when that is over 64 KiB, the file named by
INPUT_JSON_PATH. `--input FILE` reads them from a file instead (tests, local runs).

Standard library only. Prints exactly one JSON object on stdout, valid against
assets/output.schema.json, and writes the same object to
/tmp/github-activity-counts.json. On bad input it still prints a valid object and exits 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any

DEFAULT_OUT = "/tmp/github-activity-counts.json"
_REPO_IN_URL = re.compile(r"github\.com/(?:repos/)?([^/\s]+/[^/\s]+?)(?:/|$)", re.IGNORECASE)


def read_inputs(path: str | None) -> dict[str, Any]:
    if path:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    file_path = os.environ.get("INPUT_JSON_PATH")
    if file_path:
        with open(file_path, encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(os.environ.get("INPUT_JSON") or "{}")


def parse_repositories(value: Any) -> list[str]:
    parts = re.split(r"[\s,]+", value) if isinstance(value, str) else value
    if not isinstance(parts, list):
        return []
    seen: dict[str, str] = {}
    for part in parts:
        name = str(part).strip()
        if "/" in name:
            seen.setdefault(name.lower(), name)
    return list(seen.values())


def find_search(value: Any) -> dict[str, Any] | None:
    """The object holding `items`, however the step's output wraps it."""
    if isinstance(value, dict):
        if isinstance(value.get("items"), list):
            return value
        for child in value.values():
            found = find_search(child)
            if found is not None:
                return found
    return None


def repo_of(item: dict[str, Any]) -> str | None:
    for key in ("repository_url", "html_url", "url"):
        url = str(item.get(key) or "").replace("api.github.com", "github.com")
        match = _REPO_IN_URL.search(url)
        if match:
            return match.group(1).lower()
    return None


def parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def count_search(
    raw: Any,
    wanted: dict[str, str],
    stamp_of: Any,
    window: tuple[datetime, datetime] | None,
) -> tuple[dict[str, int] | None, bool, dict[str, int]]:
    """Returns (counts per repo or None if unreadable, complete, what was left out)."""
    search = find_search(raw)
    if search is None:
        return None, False, {}
    counts = dict.fromkeys(wanted.values(), 0)
    left_out = {"other_repository": 0, "wrong_state": 0, "outside_window": 0}
    seen: set[tuple[str, Any]] = set()
    for item in search["items"]:
        if not isinstance(item, dict):
            continue
        repo = repo_of(item)
        if repo is None or repo not in wanted:
            left_out["other_repository"] += 1
            continue
        key = (repo, item.get("number"))
        if key in seen:
            continue
        seen.add(key)
        stamp = stamp_of(item)
        if stamp is None:
            left_out["wrong_state"] += 1
            continue
        moment = parse_time(stamp)
        if window and moment and not window[0] <= moment <= window[1]:
            left_out["outside_window"] += 1
            continue
        counts[wanted[repo]] += 1
    total = search.get("total_count")
    returned = len(search["items"])
    complete = not search.get("incomplete_results") and not (
        isinstance(total, int) and total > returned
    )
    return counts, complete, left_out


def merged_stamp(item: dict[str, Any]) -> Any:
    pull = item.get("pull_request")
    return pull.get("merged_at") if isinstance(pull, dict) else None


def opened_stamp(item: dict[str, Any]) -> Any:
    return item.get("created_at") if item.get("state") == "open" else None


def phrase(label: str, counts: dict[str, int] | None, repos: list[str], at_least: bool) -> str | None:
    if counts is None:
        return None
    total = sum(counts.values())
    text = f"{'at least ' if at_least else ''}{total} {label}"
    if len(repos) > 1:
        text += " (" + ", ".join(f"{counts[r]} in {r}" for r in repos) + ")"
    return text


def sentence_of(
    merged: dict[str, int] | None,
    opened: dict[str, int] | None,
    repos: list[str],
    merged_complete: bool,
    opened_complete: bool,
) -> str:
    if merged is None and opened is None:
        return ""
    if not sum((merged or {}).values()) and not sum((opened or {}).values()) and merged is not None and opened is not None:
        return "No PRs merged or opened."
    total_merged = sum((merged or {}).values())
    merged_label = "PR merged" if total_merged == 1 else "PRs merged"
    first = phrase(merged_label, merged, repos, not merged_complete)
    second = phrase("opened" if first else "PRs opened", opened, repos, not opened_complete)
    text = " and ".join(part for part in (first, second) if part) + "."
    return text[0].upper() + text[1:]


def build(inputs: dict[str, Any]) -> tuple[dict[str, Any], int]:
    repos = parse_repositories(inputs.get("repositories"))
    if not repos:
        return (
            {
                "outcome": "error",
                "repos": [],
                "totals": {"merged": None, "opened": None},
                "complete": False,
                "sentence": "",
                "unknowns": ["repositories is missing or names no owner/repo."],
            },
            1,
        )
    wanted = {repo.lower(): repo for repo in repos}
    now = parse_time(inputs.get("now"))
    hours = inputs.get("lookback_hours")
    window = (now - timedelta(hours=hours), now) if now and isinstance(hours, int) else None

    merged, merged_complete, merged_out = count_search(inputs.get("merged"), wanted, merged_stamp, window)
    opened_given = inputs.get("opened") is not None
    opened, opened_complete, opened_out = count_search(inputs.get("opened"), wanted, opened_stamp, window)

    unknowns: list[str] = []
    if merged is None:
        unknowns.append("The merged pull request search could not be read, so there is no merged count.")
    elif not merged_complete:
        unknowns.append("The merged pull request search returned fewer results than it found, so that count may be too low.")
    if opened is None:
        unknowns.append(
            "The opened pull request search could not be read, so there is no opened count."
            if opened_given
            else "No opened pull request search was given, so there is no opened count."
        )
    elif not opened_complete:
        unknowns.append("The opened pull request search returned fewer results than it found, so that count may be too low.")
    for name, out in (("merged", merged_out), ("opened", opened_out)):
        if out.get("other_repository"):
            unknowns.append(f"Left out {out['other_repository']} {name} results from other repositories.")
        if out.get("wrong_state"):
            unknowns.append(f"Left out {out['wrong_state']} {name} results that were not {name}.")
        if out.get("outside_window"):
            unknowns.append(f"Left out {out['outside_window']} {name} results outside the window.")

    complete = merged is not None and opened is not None and merged_complete and opened_complete
    output = {
        "outcome": "ok" if complete else "partial",
        "repos": [
            {
                "repo": repo,
                "merged": merged[repo] if merged is not None else None,
                "opened": opened[repo] if opened is not None else None,
            }
            for repo in repos
        ],
        "totals": {
            "merged": sum(merged.values()) if merged is not None else None,
            "opened": sum(opened.values()) if opened is not None else None,
        },
        "complete": complete,
        "sentence": sentence_of(merged, opened, repos, merged_complete, opened_complete),
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
