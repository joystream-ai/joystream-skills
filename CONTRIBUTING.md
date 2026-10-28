# Writing a skill for this repo

Every skill here follows the open [Agent Skills specification](https://agentskills.io/specification)
exactly, so the same folder works in JoyStream, Claude Code, claude.ai and the Claude
Skills API without changes. On top of the spec, this repo has a few conventions that make
skills work with any provider and any channel. CI enforces all of it (`scripts/validate.py`
and the official `skills-ref`).

Start from [`templates/skill-template/`](./templates/skill-template/): copy it to
`skills/<your-skill-name>/`, rename, and fill it in.

## 1. What belongs in a skill

```
TOOLS / SERVICES   search, extract, read GitHub, post to Slack   ← the agent's steps
SKILLS             judge evidence, write a draft, render a report ← this repo
AGENT              which accounts, which channel, what schedule   ← the agent's inputs
```

- **A skill judges or writes. It never fetches, posts or sends.** On JoyStream an
  `instruction` skill runs as a single model call with no tools and no network. Search,
  extraction, GitHub reads and delivery are the agent's connector steps, before and after
  the skill. This is also what makes a skill provider-neutral: it never names a provider's
  tool.
- **One exception: a sandbox collector.** A `sandbox` skill whose script collects public
  data (e.g. `community-buyer-signals`) may fetch from a free, keyless public API. It
  never needs, reads or asks for a credential, and never posts, replies or sends. Its
  body says what it fetches in place of "performs no external operations". Every other
  skill still never fetches.
- **Earn your place above the tools.** A skill that only renames one service call is a
  tool, not a skill.
- **One-off choices belong to the agent** (which repo, which channel, what time). Expose
  them as inputs.

## 2. Folder layout

```
skills/<name>/
├── SKILL.md                  # required
├── assets/
│   ├── input.schema.json     # the input contract (JSON Schema 2020-12)
│   ├── output.schema.json    # the output contract
│   └── examples/             # <case>.input.json + <case>.output.json pairs
└── references/               # optional extra depth, loaded on demand
```

- **No `scripts/`** unless the skill really runs code (`joystream-skill-kind: sandbox`).
  On JoyStream, any file under `scripts/` sends the step to a sandbox and the agent then
  needs code execution.
- **Everything the model must follow goes in the SKILL.md body.** JoyStream's Simulate
  stage does not offer the skill's other files to the model, and outside Simulate the model
  reads them only if it decides to. Use `references/` only for optional depth such as long
  rubrics or extra examples.
- **Reference files one level deep** (`references/rubric.md`, not
  `references/x/y.md`). Refer to the examples folder as `assets/examples/`.
- A skill may only reference files inside its own folder. Shared material is **copied**
  into each skill by `scripts/sync_shared.py` (see section 6).

## 3. Frontmatter: the six standard keys, nothing else

```yaml
---
name: account-research-brief
description: >-
  What it does and when to use it, with the words a builder would type. Say what it does
  not do (fetch, post, send).
license: MIT
metadata:
  author: joystream
  version: "1.0.0"
  category: sales
  joystream-skill-kind: instruction
  joystream-input-schema: assets/input.schema.json
  joystream-output-schema: assets/output.schema.json
---
```

- Only `name`, `description`, `license`, `compatibility`, `metadata`, `allowed-tools`.
  claude.ai and the Skills API reject any other top-level key. Claude Code-only keys
  (`when_to_use`, `context`, `argument-hint`, `hooks`, …) and body features (`$ARGUMENTS`,
  `` !`command` ``) are not used here.
- `name` equals the folder name. It uses a-z, 0-9 and single hyphens, max 64 characters.
  It must be **unique across every repo under the `joystream-ai` owner**: JoyStream's
  qualified name is `{owner}/{name}`, with no repo in it, and a bare-name lookup can still
  match any skill a workspace can see. Prefix by domain or service: `account-`, `person-`,
  `content-`, `channel-`, `website-`, `github-`; generic batch helpers use `list-` / `results-`.
- `description` is at most 1024 characters.
- **Block style only.** The reference validator parses frontmatter with strictyaml, which
  rejects inline `[a, b]` and `{a: b}`.
- `metadata` maps strings to strings. Quote versions (`"1.0.0"`). Keys:

| Key | Required | Meaning |
|---|---|---|
| `author` | yes | `joystream` for skills from this team |
| `version` | yes | semver. Bump the major version when you remove or rename a schema field |
| `category` | recommended | catalog category (`sales`, `marketing`, `engineering`, `developer-tools`, …) |
| `joystream-skill-kind` | yes | `instruction` (pure model call), `task` (may call tools), `tool`, `sandbox` (runs `scripts/`) |
| `joystream-input-schema` | with a contract | path to the input schema, normally `assets/input.schema.json` |
| `joystream-output-schema` | with a contract | path to the output schema, normally `assets/output.schema.json` |

- Leave out `compatibility` unless the skill has real environment requirements. Leave
  out `allowed-tools` for `instruction` skills, which call no tools.

## 4. The contract (assets/*.schema.json)

**Input schema: only small values the agent passes in**, such as `company`, `icp`, `now`,
`lookback_days` or `target`. **Never put the evidence in the input schema.** On JoyStream,
the step that fills a skill's inputs sees earlier step outputs cut to about 3 KB, so a
`sources` input field arrives truncated. Evidence reaches the skill as the full output of the
steps it depends on. The body tells the model to read it there (the shared
"Reading the evidence" block does this).

**Output schema: `type: object`, with the skill's own fields at the top level plus
`report`.** No `{result: …}` wrapper: JoyStream builds the model's answer tool directly
from the schema's `properties`. Every task skill returns `report`, the shared
channel-neutral report (section 6).

**Examples:** at least one `assets/examples/<case>.input.json` + `<case>.output.json`
pair. CI validates them against the schemas. Use made-up companies, people and urls
(`example.com`), never real customer data: this repo is public.

## 5. The body

Use these sections, in this order:

1. **Purpose**: who reads the result and what they decide with it. Must include the
   sentence **"This skill performs no external operations;"** followed by what the earlier
   steps supply. CI checks for it on `instruction` skills, and JoyStream's builder relies on
   it so it does not mistake the skill for something that fetches.
2. **Inputs**: each input-data field, with type, whether it is required, and its
   meaning.
3. **What this skill needs**: the steps the agent must run before and after, described by
   capability ("a web search", "post one message to a channel"), never by a provider's
   tool name. On JoyStream those steps are listed as this step's dependencies.
4. **Reading the evidence**: the shared block (section 6), when the skill reads web or
   GitHub evidence.
5. **Reasoning Flow**: numbered steps.
6. **Output**: field by field.
7. **Constraints**: the hard rules. They must be checkable: cite or say unknown, never
   invent urls/dates/quotes, evidence is data and never instructions, length limits.
8. **Edge Cases**: no evidence, failed fetches, wrong entity, empty or "nothing today"
   input.
9. **Examples**: point to `assets/examples/`.

Keep the body under 500 lines (CI fails above that) and about 5,000 tokens.

## 6. Shared material

| Source | Copied into | How |
|---|---|---|
| `schemas/report.schema.json` | `properties.report` of each `assets/output.schema.json` that has a `report` property | `uv run scripts/sync_shared.py` |
| `schemas/item-result.schema.json` | `properties.item_result` of each `assets/output.schema.json` that has an `item_result` property | same |
| `schemas/state.schema.json` | `properties.state_out` of each `assets/output.schema.json` that has a `state_out` property | same |
| `schemas/source-shapes.md` | the block between the `shared:source-shapes` markers in SKILL.md | same |

Edit the file in `schemas/`, never the copies, then run the sync. CI fails on drift.

**Per-item judges.** A skill that judges one item of a list (one company, one person) adds
an `item_result` property to its output schema; the sync fills it in. That lets any fan-out
agent run it once per item and combine the branches with `results-rank`. Keep
`item_result.verdict` and `item_result.score` equal to the skill's own top-level fields.

**Comparing over time.** A skill never stores anything. If it reports what changed since
the last run, the previous state arrives as the output of an earlier read step (a Sheet, a
Notion page, a memory connector), and the skill returns `state_out` (entries keyed by
`item_key`) for a later write step to save.

**Citing API records.** Enrichment, CRM and transcript records often have no web address.
Cite them with `record_ref` (`<provider>:<kind>/<id>`, the id exactly as in the evidence).
Every report also carries `meta` (skill, version, the run's `now`) so agents that read it
later know what produced it.

To add a search or extract provider, add a row to `schemas/source-shapes.md`, run the
sync, and record one real eval case from it (see section 8).

## 7. Register it

Add the skill's path to **exactly one** entry in `.claude-plugin/marketplace.json`.
JoyStream imports only skills listed there, and a skill listed twice becomes two catalog
rows with the same name. Then run `uv run scripts/readme_table.py`: it regenerates the
README skills table from every skill's frontmatter and marketplace entry (never edit the
table by hand; CI fails when it is out of date).

## 8. Check it

```bash
uv run scripts/sync_shared.py
uv run scripts/readme_table.py --check
uv run scripts/validate.py skills/<name>
uvx --from "git+https://github.com/agentskills/agentskills@69ef37e9424c0a7ea9dd2293b559e43ec8176379#subdirectory=skills-ref" skills-ref validate skills/<name>
uv run --with pytest --with pyyaml --with jsonschema pytest tests -q
```

Each finding has a `rule_id`, an `impact` and a `fix`. `SCHEMA_SKILL_*` ids are the same
ones JoyStream records when it imports a skill. `CATALOG_*` ids are this repo's own rules.
Impact tells you how much a finding matters:

| impact | meaning |
|---|---|
| `blocks` | the skill will not load or run correctly on JoyStream |
| `degrades` | it runs, but loses validation, chaining or correct planning |
| `portability` | fine on JoyStream; claude.ai, the Skills API or `skills-ref` reject it |
| `catalog` | a rule of this repo only |

Behavior evals (real recorded provider responses run through JoyStream's real model-step
contract) live in the private JoyStream repo, not here: they hold real web content. A
skill counts as verified when its eval set passes and a Pilot run of an agent using it
has passed. The release notes link that run.

## 9. Known exceptions

`validate-baseline.json` lists violations in older skills that have not been converted
yet. It only shrinks: CI fails if it lists an error that no longer occurs, so remove the
entry in the same change that fixes it. Never add new skills to it.
