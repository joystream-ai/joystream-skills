---
name: github-repo-activity-summary
description: Summarize what changed in one or more GitHub repositories over a lookback window — merged PRs, opened PRs, and notable commits — as a short, human-readable digest rather than a raw dump of titles.
compatibility: claude
license: MIT
allowed_tools:
  # Dispatched through the OpenConnector gateway's execute_action, not called
  # directly — these are the connector's own action ids (github.* namespace),
  # not github-mcp-server tool names. See the Reasoning Flow below for exactly
  # which actionId + input fields each step uses.
  - github.search_issues_and_pull_requests
  - github.list_commits
  - github.get_commit
  - github.get_pull_request
metadata:
  author: joystream
  version: "1.1"
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

**Tooling note — how dispatch actually works.** This skill runs through the
OpenConnector gateway: every step below is one `execute_action` call with a
specific `actionId` and `input` object (never call a tool literally named
`search_pull_requests` or `list_commits` — those don't exist at this layer; the
gateway only exposes `search_actions` / `get_action_guide` / `execute_action` /
`list_apps` / `list_connections`, and this skill already tells you the exact
`actionId` for every step so you never need `search_actions`/`get_action_guide`
to find them). Field names below are the connector's own (camelCase), verified
against the live catalog, not github-mcp-server's names.

1. Compute the cutoff timestamp: `since = now - lookback_hours` (UTC). Use the
   `YYYY-MM-DDThh:mm:ssZ` form GitHub search accepts.
2. For each repository, gather via the connector's `github.*` actions:
   - **Merged PRs** — `execute_action(actionId:
     "github.search_issues_and_pull_requests", input: {query: "repo:{owner}/{repo}
     is:pr is:merged merged:>={since}"})`. There is no structured `merged`-date
     filter field on this action, so this always goes through the raw `query`
     string, GitHub search syntax, not the structured `type`/`isMerged` fields.
   - **Opened PRs** — same action, `query: "repo:{owner}/{repo} is:pr is:open
     created:>={since}"`.
   - **Commits** — `execute_action(actionId: "github.list_commits", input: {owner,
     repo, sha: branch, since})` — `sha` is the branch/ref to list from, `since` is
     the cutoff. Keep only commits **not** already represented by a merged PR above
     (avoid double-counting merge commits). Use `execute_action(actionId:
     "github.get_commit", input: {owner, repo, ref})` if you need a commit's full
     message/diff to describe it (`ref` is the commit SHA).
   - **PR outcome** — when a PR title is too thin to describe the effect, read its
     body via `execute_action(actionId: "github.get_pull_request", input: {owner,
     repo, pullNumber})` rather than restating the title.
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
