---
name: github-repo-activity-summary
description: Summarize what changed in one or more GitHub repositories over a lookback window — merged PRs, opened PRs, and notable commits — as a short, human-readable digest rather than a raw dump of titles.
compatibility: claude
license: MIT
allowed_tools:
  - github
  # Backed by the official github/github-mcp-server tools:
  #   search_pull_requests, list_commits, pull_request_read, get_commit
metadata:
  author: joystream
  version: "1.0"
  category: developer-tools
---

# Purpose

Produce a concise, editorial summary of recent activity in one or more GitHub
repositories. This is the "repo changes" half of a team news digest: it answers
*"what did we ship and what's in flight?"* — **not** a copy-paste of every PR
title. It only reads and summarizes — it returns a digest and posts nowhere, so
the agent that runs it chooses the destination. Reusable on its own or alongside
`github-project-ticket-summary`.

# Inputs

- **repositories** (list of `owner/repo`, required): the repos to scan.
  Defaults for JoyStream: `joystream-ai/joystream`, `joystream-ai/llm-wiki`.
- **lookback_hours** (integer, default `24`): only include activity newer than
  `now - lookback_hours`.
- **branch** (string, default repo default branch): branch to consider for commits.

# Reasoning Flow

1. Compute the cutoff timestamp: `since = now - lookback_hours` (UTC). Use the
   `YYYY-MM-DDThh:mm:ssZ` form GitHub search accepts.
2. For each repository, gather via the official `github` MCP tools:
   - **Merged PRs** — `search_pull_requests` with
     `repo:{owner}/{repo} is:pr is:merged merged:>={since}`. (Do **not** use
     `list_pull_requests` for this — it has no date filter, only state/head/base/sort,
     so it cannot honor the window.)
   - **Opened PRs** — `search_pull_requests` with
     `repo:{owner}/{repo} is:pr is:open created:>={since}`.
   - **Commits** — `list_commits` with `sha={branch}` and `since={since}`, keeping
     only commits **not** already represented by a merged PR above (avoid
     double-counting merge commits). Use `get_commit` if you need a commit's full
     message/diff to describe it.
   - **PR outcome** — when a PR title is too thin to describe the effect, read its
     body via `pull_request_read` (`method: get`) rather than restating the title.
3. **Summarize, don't list.** Group related work into themes (e.g. "auth", "billing",
   "catalog resolver"). For each theme write one plain-language sentence describing
   the *outcome* — what a teammate needs to know — citing PR/commit numbers in
   parentheses. Collapse trivial commits (typo fixes, version bumps, formatting)
   into a single "housekeeping" line or omit them.
4. Return a compact markdown block plus a structured object for downstream use.

**Trigger-aware behavior:** if `trigger.type == "manual"`, return the digest to the
invoking user for review instead of treating it as final. For `cron` / `agent_call`
triggers, return the finished digest directly.

# Output

A markdown block per repository:

```
**joystream-ai/joystream**
- Shipped connect-time credential grant so credentials show up immediately on connect (#518, #527).
- Scoped the cron trigger-binding unique index to cron rows only, fixing a duplicate-binding crash (#528).
- In flight: MCP discovery for Mode-B direct authoring (#498, open).
- Housekeeping: 3 dependency bumps, formatting.
```

Plus a structured object:
- `repo` (string)
- `merged_prs[]`, `opened_prs[]` (each: number, title, url, author)
- `themes[]` (each: `label`, `summary`, `refs[]`)
- `housekeeping_count` (integer)

# Constraints

- **Summarize the work, do not restate PR titles verbatim.** A reader should learn
  the *effect* of a change, not just its label.
- Never invent PR numbers, authors, or outcomes. Every claim must trace to a real
  PR/commit in the window; cite its number.
- Keep each repo's section to at most ~6 bullets. If there is more, prioritize
  merged/shipped work over open PRs, and features/fixes over chores.
- Do not include bot commits (dependabot, renovate) except as an aggregate
  housekeeping count.

# Edge Cases

## No activity in the window
Return a single line for that repo: `**owner/repo** — no notable activity in the last {lookback_hours}h.`

## Very high volume (>25 PRs/commits)
Do not enumerate everything. Summarize the top themes by impact and end with a count:
"…and 14 more smaller changes." Never truncate silently without saying you did.

## Rate limited / repo unreachable
Return the repos you could read, and add an explicit line noting which repo(s) failed
and why (rate limit vs not found) so the digest is not silently incomplete.

# Examples

## Two repos, normal day
**Input:** `repositories: [joystream-ai/joystream, joystream-ai/llm-wiki]`, `lookback_hours: 24`.
**Output:** One themed section per repo; joystream has 4 merged PRs grouped into 2
themes + housekeeping; llm-wiki has a "no notable activity" line.
