---
name: github-project-ticket-summary
description: Summarize ticket movement on a GitHub Project over a lookback window — what closed, what moved status, what got opened — with a one-line gist of the actual work behind each closed ticket instead of just its title.
compatibility: claude
license: MIT
allowed_tools:
  # Dispatched through the OpenConnector gateway's execute_action, not called
  # directly — these are the connector's own action ids (github.* namespace),
  # not github-mcp-server tool names. See the Reasoning Flow below for exactly
  # which actionId + input fields each step uses.
  - github.list_projects
  - github.list_project_items
  - github.search_issues_and_pull_requests
  - github.get_issue
  - github.get_pull_request
metadata:
  author: joystream
  version: "1.1"
  category: developer-tools
---

# Purpose

Produce a concise summary of ticket activity on a GitHub Project. This is the
"ticket changes" half of a team news digest: it answers *"what did we finish and
what's newly on the board?"* — e.g. "closed 5 tickets yesterday, here's the gist
of each." It only reads and summarizes — it returns a digest and posts nowhere, so
the agent that runs it chooses the destination. Reusable on its own or alongside
`github-repo-activity-summary`.

# Inputs

- **project** (string, required): the GitHub Project to read. For JoyStream this is
  the org project named **"development project"** (resolve its project number via
  `github.list_projects`, below).
- **repositories** (list of `owner/repo`, optional): scope the project's items to
  these repos when the project spans several. Default for JoyStream:
  `joystream-ai/joystream`, `joystream-ai/llm-wiki`.
- **lookback_hours** (integer, default `24`): the window for issue closures and new
  issues (from each issue's `closed_at` / `created_at`). Current board status is
  read as-is and is not time-windowed — see the Reasoning Flow tooling note.

# Reasoning Flow

**Tooling note — how dispatch actually works.** This skill runs through the
OpenConnector gateway: every step below is one `execute_action` call with a
specific `actionId` and `input` object (never call a tool literally named
`search_issues` or `projects_list` — those don't exist at this layer; the gateway
only exposes `search_actions` / `get_action_guide` / `execute_action` /
`list_apps` / `list_connections`, and this skill already tells you the exact
`actionId` for every step so you never need `search_actions`/`get_action_guide`
to find them). Field names below are the connector's own (camelCase — e.g.
`issueNumber`, not `issue_number`), verified against the live catalog, not
github-mcp-server's names.

**Tooling note — where the window actually comes from.** GitHub Projects v2 (and
`github.list_project_items`) expose an item's **current** field values, but **no
per-field change history**: you cannot learn *when* a status flipped or *which*
field changed. So do not try to time-window off the board. Derive the window
from the **underlying issues** (which carry `closed_at` / `created_at`), and use
the project only to **scope** (which issues are on the board) and to read
**current** status.

1. Compute the cutoff: `since = now - lookback_hours` (UTC), formatted as
   `YYYY-MM-DDThh:mm:ssZ`.
2. Resolve the project number: `execute_action(actionId: "github.list_projects",
   input: {owner, ownerType: "org", query: project})`. Match by title. Then read
   its items and current status with `execute_action(actionId:
   "github.list_project_items", input: {owner, ownerType: "org", projectNumber,
   fieldNames: ["Status"]})` — `fieldNames` is required to get status back; without
   it you only get titles. This is the **scope set**: the issues currently on the
   board and their current status. Restrict to `repositories` when given.
3. Build each bucket from issue timestamps, not board movement:
   - **Closed / Done** — `execute_action(actionId:
     "github.search_issues_and_pull_requests", input: {query: "repo:{owner}/{repo}
     is:issue is:closed closed:>={since}"})`, intersected with the scope set. This
     is the accurately time-windowed set.
   - **Opened / Added** — same action, `query: "repo:{owner}/{repo} is:issue
     is:open created:>={since}"`, intersected with scope.
   - **In progress** — issues in the scope set whose **current** board status
     (from step 2) is a non-terminal in-progress state. Report this as *current
     board state*, **not** as "moved within the window" — the tools cannot prove
     a transition time. Omit precise from→to movement claims.
4. **For each closed ticket, write a one-line gist of the work**, not just the
   title. Call `execute_action(actionId: "github.get_issue", input: {owner, repo,
   issueNumber})` to find its linked/closing PR, then `execute_action(actionId:
   "github.get_pull_request", input: {owner, repo, pullNumber})` for the PR body —
   or the issue's final comments — to describe what was actually done and why it
   mattered. Cite the ticket number and any linked PR. Never fabricate.
5. Lead with a headline count ("Closed 5 tickets, 3 in progress, opened 2") then
   the details.
6. Return markdown plus a structured object.

**Trigger-aware behavior:** on `trigger.type == "manual"`, return the draft for review;
on `cron` / `agent_call`, return the finished digest directly.

# Output

```
**Development Project — last 24h**
Closed 5 · In progress 3 · Opened 2

Closed
- #528 Cron unique-index bug — scoped the index to cron rows so duplicate bindings
  can't crash trigger creation.
- #517 Agent creation in one go — Lanes A/B/D plus BFF and session-nav fixes landed.
- …

In progress (current board state)
- #405 MCP discovery — currently In Review; Phase 1 authoring shipped.

Newly opened
- #531 Digest agent for team news.
```

Plus a structured object:
- `counts` (`closed`, `in_progress`, `opened`)
- `closed[]` (each: number, title, gist, refs[])
- `in_progress[]` (each: number, title, current_status)
- `opened[]` (each: number, title)

# Constraints

- **Every closed ticket gets a gist of the real work**, sourced from its PR/commit/
  comments — never fabricated. If no source explains the work, say
  "closed (no linked PR / description)" rather than guessing.
- Do not restate ticket titles as the whole summary; the title plus a substantive
  gist is the minimum.
- Keep the closed section to the tickets that actually closed in the window; do not
  pull in older done items.
- If the project spans repos beyond the requested scope, exclude out-of-scope items
  and note that you scoped the view.
- **Never claim status transitions the tools cannot prove.** `github.
  list_project_items` gives current status only, not change history — report "in
  progress" as current board state, and do not assert from→to movement or a
  movement time.

# Edge Cases

## No closures or new tickets in the window
Return: `**Development Project** — no ticket changes in the last {lookback_hours}h.`
(Base this on closed/opened issue timestamps, not the board — the board has no
per-item change timing.)

## Ticket closed without a linked PR
Include it with the gist marked `closed (no linked PR)`; do not omit it and do not invent work.

## Project not found / not accessible
`github.list_projects` returning no match, or `github.list_project_items` erroring,
means the project isn't reachable with the current connection. Return an explicit
failure line naming the project and the reason, so the composing digest can flag
that the ticket half is missing rather than appear empty.

# Examples

## Busy sprint day
**Input:** `project: "development project"`, `lookback_hours: 24`.
**Output:** Headline "Closed 5 · In progress 3 · Opened 2", each closed ticket with a
one-line gist sourced from its merged PR, followed by in-progress (current board
state) and newly opened lists.
