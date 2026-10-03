# JoyStream Skills

A public, open collection of [Agent Skills](https://agentskills.io) for
[JoyStream](https://joystream.ai) — reusable building blocks you can import into
your JoyStream Skills Catalog and compose into agents.

Each skill is a directory containing a `SKILL.md` (YAML frontmatter + a Markdown
body). Skills and plugins are registered in `.claude-plugin/marketplace.json`; the
JoyStream importer follows that manifest.

## Repo layout & naming

```
joystream-skills/
├── .claude-plugin/marketplace.json   # registers every plugin and standalone skill
├── skills/                           # standalone skills, one directory each
│   ├── github-repo-activity-summary/SKILL.md
│   └── github-project-ticket-summary/SKILL.md
└── plugins/                          # a plugin = one use case composing >1 skill
    └── <name>/
        ├── .claude-plugin/plugin.json
        └── skills/<skill>/SKILL.md
```

- **Standalone skill**: one reusable capability or judgment. Goes in `skills/<name>/`
  and is listed under a use-case entry in `marketplace.json` (today: `github-reports`).
- **Plugin**: a use case that needs more than one skill. Goes in `plugins/<name>/` with
  its own `plugin.json` and gets its own `marketplace.json` entry. See
  [`plugins/README.md`](./plugins/README.md).

A skill's directory name must equal its frontmatter `name`. Names are
service-prefixed (`github-…`) so they stay unique and self-describing. Catalog
identity is `{repo-owner}/{name}`, e.g. `joystream-ai/github-repo-activity-summary`.

## Skills

| Skill (`name`) | What it does | Services |
|----------------|--------------|----------|
| [`github-repo-activity-summary`](./skills/github-repo-activity-summary/SKILL.md) | Reads merged/open PRs + notable commits across one or more repos over a window and returns a **summarized** "repo changes" digest | GitHub |
| [`github-project-ticket-summary`](./skills/github-project-ticket-summary/SKILL.md) | Reads closed/opened tickets (time-windowed) plus current in-progress board state on a GitHub Project and returns a **summarized** "ticket changes" digest, with a gist of each closed ticket | GitHub |
| [`platform-alert-diagnosis`](./skills/platform-alert-diagnosis/SKILL.md) | Diagnoses one JoyStream platform alert from js-monitor with read-only Railway reads and posts **one** diagnosis in the alert's Slack thread | Railway, Slack |
| [`github-release-digest`](./skills/github-release-digest/SKILL.md) | Summarizes a repo's releases published between a start and end date as a customer-friendly announcement and posts **one** colored message to a Slack channel | GitHub, Slack |

The `github-repo-activity-summary` and `github-project-ticket-summary` skills only **read and summarize** — they return a digest and post nowhere,
so they compose with any delivery service (Discord, Slack, email) and can be
reused independently or together. Delivery is handled by the *agent* calling a
connected service, not by a skill.

## Import into JoyStream

```bash
# CLI
joystream skills repo add https://github.com/joystream-ai/joystream-skills \
  --scope selected --workspace <workspace-id>

joystream skills list        # confirm the skills landed
```

Or in the app: **Settings → Skill Repositories → Add repository**, paste this
repo's URL, and pick a visibility scope.

## How we think about skills

Skills sit in a layered model — pick the altitude that matches the value a skill adds:

```
TOOLS / SERVICES   raw ops (github.listPRs) — already generic; the agent's services
       ↑ a skill must add reusable PROCEDURE or JUDGMENT above this line
SKILLS             capability skill  → a reusable primitive (read + normalize)
                   task skill        → a reusable judgment (summarize into a digest)
       ↑
AGENT              one-off glue: which repos, which channel, what schedule
```

Guidelines for contributing a skill here:

- **Earn your place above the tools.** If a skill is just a rename of one service
  call with no added procedure or judgment, it's a tool, not a skill.
- **Generic for capabilities, specific for judgment.** Make reusable *capabilities*
  broad and parameterized; a *judgment* (like "summarize into themes") is meant to
  be opinionated — keep it as its own task skill only if more than one agent reuses it.
- **One-off logic belongs in the agent, not a skill** (target repos, `#channel`,
  cron time). Keep those out of `SKILL.md`; expose them as `Inputs`.
- **Standalone skills go in `skills/`, multi-skill use cases in `plugins/`.** Give each
  skill a **service-prefixed `name`** matching its directory, and register it in
  `marketplace.json`.
- **One skill per directory**, `SKILL.md` with frontmatter: `name`, `description`,
  `compatibility`, `license`, `allowed_tools`, `metadata`. Body follows the
  Purpose / Inputs / Reasoning Flow / Output / Constraints / Edge Cases / Examples
  shape used by the skills here.

## License

MIT — see [LICENSE](./LICENSE).
