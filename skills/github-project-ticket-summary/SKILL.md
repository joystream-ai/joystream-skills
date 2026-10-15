---
name: github-project-ticket-summary
description: Summarize ticket movement on a GitHub Project over a lookback window — what closed, what is in progress, what got opened — with a one-line gist of the actual work behind each closed ticket instead of just its title.
license: MIT
metadata:
  author: joystream
  version: "2.1.0"
  category: developer-tools
  joystream-skill-kind: instruction
  joystream-input-schema: assets/input.schema.json
  joystream-output-schema: assets/output.schema.json
---

# Purpose

Produce a concise summary of ticket activity on a GitHub Project. This is the
"ticket changes" half of a team news digest: it answers *"what did we finish and
what's newly on the board?"* — e.g. "closed 5 tickets yesterday, here's the gist of
each."

This skill performs no external operations; the agent's earlier steps read the board,
the issues closed and opened in the window and their linked PRs (see *Where the data
comes from*), and a later step posts the digest. It summarizes only. It fetches nothing
and posts nowhere, so the agent that runs it
chooses both the source scope and the destination. Reusable on its own or alongside
`github-repo-activity-summary`.

# You have no tools

**Do not attempt any tool call. There is none available to you, and a tool call is
not the way this skill does its job.**

Every GitHub read happens in its own recipe step *before* this one, and those steps
pass their results in as the inputs above. Your entire job is to turn those inputs
into the digest.

If an input is missing or empty, that is a fact about the upstream step — report it
as described under *Edge Cases*. Never respond that you need a tool, a connector, or
API access; never ask to be given one. A missing input is reported, not retried.

# Inputs

Declared in full in `assets/input.schema.json`. In prose:

- **project_items** — the board's current contents. This is the **scope set**: a
  ticket is reportable only if it appears here.
- **closed_issues** / **opened_issues** — already windowed by the caller from each
  issue's `closed_at` / `created_at`.
- **lookback_hours** — the window size, for the headline. Not a filter you apply.
- **repositories** — the owner/repo scope the caller used, when it narrowed one.
- **trigger_type** — `manual` means a person is watching; `cron` / `agent_call` mean
  the output ships as-is.

# Where the data comes from

Provided by the steps ahead of this one. An agent composing this skill needs roughly:

| Step | Provides |
|---|---|
| resolve the window | `lookback_hours` |
| find the board | the project number |
| read the board | `project_items`, with current `Status` per item |
| find closures in the window | `closed_issues` |
| find new tickets in the window | `opened_issues` |
| enrich each closure with its PR | `linked_pr` per closed issue |
| **write the digest** | **this skill** |
| post it | — |

Each of those steps binds its own arguments. A step that leaves a required argument
unbound fails before this skill is reached.

# Reasoning Flow

**Where the window comes from.** GitHub Projects v2 exposes an item's **current**
field values and **no per-field change history** — nothing upstream can learn *when*
a status flipped or *which* field changed. So the window never comes from the board.
It comes from the underlying issues' timestamps, which the caller has already applied.
The board is used only to **scope** (which issues count) and to read **current**
status.

1. **Bucket; do not re-filter.** `closed_issues` and `opened_issues` arrive already
   windowed. Do not re-check them against `lookback_hours`.
2. **Scope to the board.** Report a closed or opened issue only when it also appears
   in `project_items`. Drop out-of-scope tickets silently; if any were dropped, or if
   `project_items` spans repos outside `repositories`, set `scope_note` saying the
   view was scoped.
3. **In progress = current board state.** Take `project_items` whose `status` is in
   `in_progress_statuses`, or absent that input, any status that is neither a done nor
   a backlog column. Report these as current state, never as movement.
4. **Write a one-line gist for every closed ticket**, not just its title. Source it
   from `linked_pr.body`, else the PR title, else trailing `comments`. Say what was
   actually done and why it mattered. Cite the ticket number and the PR when there is
   one. Never fabricate.
5. **Lead with a headline count** ("Closed 5 · In progress 3 · Opened 2"), then the
   three sections in order: Closed, In progress, Newly opened.
6. Return `markdown` **and** the structured object. The caller posts the markdown
   verbatim, so it must stand on its own.

**Trigger-aware behavior:** on `trigger_type == "manual"`, return the digest as a
draft for review; on `cron` / `agent_call`, return the finished digest directly.

# Output

```
**Development Project — last 24h**
Closed 5 · In progress 3 · Opened 2

Closed
- #528 Cron unique-index bug — scoped the index to cron rows so duplicate bindings
  can't crash trigger creation. (#530)
- #517 Agent creation in one go — Lanes A/B/D plus BFF and session-nav fixes landed.
- …

In progress (current board state)
- #405 MCP discovery — currently In Review.

Newly opened
- #531 Digest agent for team news.
```

Plus the structured object declared in `assets/output.schema.json`: `counts`, `closed[]`,
`in_progress[]`, `opened[]`, and `markdown`.

# Constraints

- **Every closed ticket gets a gist of the real work**, sourced from its linked PR or
  comments — never fabricated. With no source that explains the work, write
  `closed (no linked PR / description)` rather than guessing.
- **Do not restate a ticket title as the whole entry.** Title plus a substantive gist
  is the minimum.
- **Keep the closed section to the window.** The caller windowed it; do not pull in
  older done items from the board.
- **Scope, and say when you scoped.** Exclude items outside `repositories` and record
  it in `scope_note`.
- **Never claim a status transition.** Current board status is all that exists —
  report "in progress" as current state. Never write "moved from X to Y", and never
  assert a movement time.
- **Report only what you were given.** Never describe a ticket absent from the inputs,
  and never fill a gap left by an upstream step.

# Edge Cases

## No closures or new tickets in the window
Set `empty_result: true` and counts to zero for closed/opened, and return:
`**{project_title}** — no ticket changes in the last {lookback_hours}h.`
Still list in-progress items if the board has any — a quiet day is not an empty board.

## Ticket closed without a linked PR
Include it, gist marked `closed (no linked PR)`. Do not omit it and do not invent work.

## Board data missing or empty
An empty `project_items` means the upstream board read returned nothing — the project
was unreachable or is genuinely empty, and this skill cannot tell which. Return an
explicit line naming the project and saying the board could not be read, so the
composing digest flags the ticket half as missing rather than rendering a confident
"no changes". Do not fall back to reporting closures unscoped.

## An issue appears in both closed and opened
It was opened and closed inside the same window. List it under Closed only, and note
`opened and closed in window` in its gist.

# Examples

A worked pair is in `assets/examples/` (`busy-day.input.json` and
`busy-day.output.json`).

## Busy sprint day
**Input:** `lookback_hours: 24`, 12 `project_items`, 5 `closed_issues` (4 with
`linked_pr`), 2 `opened_issues`.
**Output:** headline `Closed 5 · In progress 3 · Opened 2`; each closed ticket with a
one-line gist sourced from its merged PR, the fifth marked `closed (no linked PR)`;
in-progress as current board state; the two new tickets by number and title.

## Quiet day
**Input:** `lookback_hours: 24`, 12 `project_items`, empty `closed_issues` and
`opened_issues`.
**Output:** `empty_result: true`, counts `0 / 3 / 0`, the no-changes line, and the
three in-progress items.
