---
name: github-project-ticket-summary
description: Summarize ticket movement on a GitHub Project over a lookback window — what closed, what moved status, what got opened — with a one-line gist of the actual work behind each closed ticket instead of just its title.
compatibility: claude
license: MIT
allowed_tools:
  # Tools from the official github/github-mcp-server:
  - search_issues        # time-windowed closures / new issues
  - issue_read           # gist + linked PR
  - pull_request_read    # gist from the merged PR
  - projects_list        # board scoping
  - projects_get         # current status / field detail
metadata:
  author: joystream
  version: "1.0"
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
  the org project named **"development project"** (resolve its project number/URL).
- **repositories** (list of `owner/repo`, optional): scope the project's items to
  these repos when the project spans several. Default for JoyStream:
  `joystream-ai/joystream`, `joystream-ai/llm-wiki`.
- **lookback_hours** (integer, default `24`): the window for issue closures and new
  issues (from each issue's `closed_at` / `created_at`). Current board status is
  read as-is and is not time-windowed — see the Reasoning Flow tooling note.

# Reasoning Flow

**Tooling note — where the window actually comes from.** GitHub Projects v2 (and the
`projects_*` MCP tools) expose an item's **current** field values and an `updatedAt`,
but **no per-field change history**: you cannot learn *when* a status flipped or
*which* field changed. So do not try to time-window off the board. Derive the window
from the **underlying issues** (which carry `closed_at` / `created_at`), and use the
project only to **scope** (which issues are on the board) and to read **current**
status.

1. Compute the cutoff: `since = now - lookback_hours` (UTC), formatted as
   `YYYY-MM-DDThh:mm:ssZ`.
2. Resolve the project and read its items with `projects_list` (and `projects_get`
   for field/status detail) to build the **scope set**: the issues currently on the
   board and their current status. Restrict to `repositories` when given.
3. Build each bucket from issue timestamps, not board movement:
   - **Closed / Done** — `search_issues` with
     `repo:{owner}/{repo} is:issue is:closed closed:>={since}`, intersected with the
     scope set. This is the accurately time-windowed set.
   - **Opened / Added** — `search_issues` with
     `repo:{owner}/{repo} is:issue is:open created:>={since}`, intersected with scope.
   - **In progress** — issues in the scope set whose **current** board status is a
     non-terminal in-progress state (from step 2). Report this as *current board
     state*, **not** as "moved within the window" — the tools cannot prove a
     transition time. Omit precise from→to movement claims.
4. **For each closed ticket, write a one-line gist of the work**, not just the title.
   Read the issue via `issue_read` (`method: get`) to find its linked/closing PR,
   then `pull_request_read` (`method: get`) for the PR body — or the issue's final
   comments — to describe what was actually done and why it mattered. Cite the ticket
   number and any linked PR. Never fabricate.
5. Lead with a headline count ("Closed 5 tickets, 3 in progress, opened 2") then the
   details.
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
- **Never claim status transitions the tools cannot prove.** The `projects_*` tools
  give current status only, not change history — report "in progress" as current
  board state, and do not assert from→to movement or a movement time.

# Edge Cases

## No closures or new tickets in the window
Return: `**Development Project** — no ticket changes in the last {lookback_hours}h.`
(Base this on closed/opened issue timestamps, not the board — the board has no
per-item change timing.)

## Ticket closed without a linked PR
Include it with the gist marked `closed (no linked PR)`; do not omit it and do not invent work.

## Project not found / not accessible
Return an explicit failure line naming the project and the reason, so the composing
digest can flag that the ticket half is missing rather than appear empty.

# Examples

## Busy sprint day
**Input:** `project: "development project"`, `lookback_hours: 24`.
**Output:** Headline "Closed 5 · In progress 3 · Opened 2", each closed ticket with a
one-line gist sourced from its merged PR, followed by in-progress (current board
state) and newly opened lists.
