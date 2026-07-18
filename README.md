# JoyStream Skills

A public, open collection of [Agent Skills](https://agentskills.io) for
[JoyStream](https://joystream.ai) — reusable building blocks you can import into
your JoyStream Skills Catalog and compose into agents.

Each skill is a directory containing a `SKILL.md` (YAML frontmatter + a Markdown
body). The JoyStream importer scans this repo's tree **recursively** and registers
every directory that has a `SKILL.md` as one skill.

## Repo layout & naming

Skills are grouped into **category folders by service** (`github/`, and more as this
grows) so the repo stays browsable:

```
joystream-skills/
└── github/
    ├── repo-activity-summary/SKILL.md
    └── project-ticket-summary/SKILL.md
```

The folder is for humans — it does **not** affect the catalog identity. A skill's
catalog identity is:

```
qualified_name = "{repo-owner}/{name-from-frontmatter}"
               = "joystream-ai/github-repo-activity-summary"
```

So collisions are avoided two ways: the **owner prefix** keeps this repo's skills
from clashing with any other org's, and each skill's `name:` is **service-prefixed**
(`github-…`) so names stay unique and self-describing within the org. Keep both when
adding skills.

## Skills

| Skill (`name`) | What it does | Services |
|----------------|--------------|----------|
| [`github-repo-activity-summary`](./github/repo-activity-summary/SKILL.md) | Reads merged/open PRs + notable commits across one or more repos over a window and returns a **summarized** "repo changes" digest | GitHub |
| [`github-project-ticket-summary`](./github/project-ticket-summary/SKILL.md) | Reads closed/opened tickets (time-windowed) plus current in-progress board state on a GitHub Project and returns a **summarized** "ticket changes" digest, with a gist of each closed ticket | GitHub |

Both skills only **read and summarize** — they return a digest and post nowhere,
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
- **Group by service** in a category folder (`github/`, `slack/`, …) and give each
  skill a **service-prefixed `name`** so catalog identities stay unique.
- **One skill per directory**, `SKILL.md` with frontmatter: `name`, `description`,
  `compatibility`, `license`, `allowed_tools`, `metadata`. Body follows the
  Purpose / Inputs / Reasoning Flow / Output / Constraints / Edge Cases / Examples
  shape used by the skills here.

## License

MIT — see [LICENSE](./LICENSE).
