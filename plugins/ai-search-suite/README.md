# ai-search-suite

The SEO Optimizer for any website. Start with **`website-seo-optimizer`**: it runs the
other six skills in order, skips any whose data is missing, and says what it skipped.
The result is a short summary card (Slack, Teams or email) and a PDF report with charts.

```
                         website-seo-optimizer  (entry point: order + skip rules)
                                     │
website-seo-opportunities  (Search Console rows → ranked opportunities, extra clicks)   optional
            │ target_urls
            ▼
website-seo-page-audit      (any domain: robots, sitemap, 30 pages of raw HTML, links)   always
            │ pages, target_urls
            ├──────────────────────────────┐
            ▼                              ▼
website-seo-performance          website-seo-content-gap                                 optional
(Lighthouse + real-user CWV)     (our page vs the pages that rank, per keyword)
            │                              │
            └──────────────┬───────────────┘
                           ▼
website-seo-brief           (ranked, copy-ready titles, metas, sections, links, JSON-LD; the card)
                           │
                           ▼
website-seo-report          (PDF + HTML: score, charts, fixes, every finding)
```

| Skill | Kind | Needs |
|---|---|---|
| [`website-seo-optimizer`](./skills/website-seo-optimizer/SKILL.md) | task | Nothing itself: sequences the skills below |
| [`website-seo-page-audit`](./skills/website-seo-page-audit/SKILL.md) | task (sandbox) | Outbound internet. Works on any public site, yours or a competitor's |
| [`website-seo-performance`](./skills/website-seo-performance/SKILL.md) | task (sandbox) | Lighthouse and Chrome (both in the JoyStream sandbox); the free PageSpeed API for real-user data |
| [`website-seo-content-gap`](./skills/website-seo-content-gap/SKILL.md) | task (sandbox) | Focus keywords and a web-search step per keyword (SerpApi, Tavily, Brave or Exa) |
| [`website-seo-opportunities`](./skills/website-seo-opportunities/SKILL.md) | task (sandbox) | Google Search Console query × page rows for the last 28 days (and the 28 before) |
| [`website-seo-brief`](./skills/website-seo-brief/SKILL.md) | instruction | The audit, and the other outputs when there are any |
| [`website-seo-report`](./skills/website-seo-report/SKILL.md) | task (sandbox) | The brief and the module outputs; Chromium (in the JoyStream sandbox) for the PDF |
| [`website-seo-changes`](./skills/website-seo-changes/SKILL.md) | task (sandbox) | The audit (speed and content gap when they ran) and the last check's snapshot, read from a Google Sheet or any store. For an SEO watch on a schedule |

## What the review covers

| Area | Checks | Skill |
|---|---|---|
| Crawl and indexing | robots.txt, sitemap, noindex, canonicals, error pages, https redirect | page audit |
| On-page | titles, meta descriptions, H1s, thin content, focus-keyword targeting | page audit |
| Links | orphan pages, internal links that 404 or redirect, redirect chains | page audit |
| International and security | hreflang codes and return links, mixed content | page audit |
| Structured data | JSON-LD validity, missing Organization; schema types competitors use | page audit, content gap |
| Speed | LCP, CLS, TBT, Lighthouse scores, real-user Core Web Vitals | performance |
| Content | subtopics, depth and FAQs the ranking pages have and ours lacks | content gap |
| Clicks | CTR gaps, striking distance, decay, cannibalization, extra clicks | opportunities |
| Progress | score change, findings fixed and new since the last audit | page audit |
| Reporting | summary card; PDF with score meter, scorecard, speed and content-gap charts | brief, report |

## What is skipped when data is missing

| Missing | What happens |
|---|---|
| Search Console (not connected, or no access to the property) | No opportunities step. The brief runs in `on_page` mode, with no click estimates, and says so |
| Lighthouse (not in the sandbox or not installed) | No speed section; the brief says speed was not measured |
| Focus keywords, or a search tool | No content gaps; the brief says which was missing |
| The PageSpeed API's real-user data (too little traffic, or a refusal) | Lab data only, said in `unknowns` |
| A page that errors, or no sitemap | Reported as a finding; the audit carries on with the pages it can read |
| The homepage | The audit returns `outcome: error`; the brief says the site could not be read instead of guessing |
| Keyword volumes, backlinks | Not part of this version: they need a paid SEO data provider (Semrush, Ahrefs, DataForSEO) |

## Use

- **Claude Code / claude.ai**: install the plugin from this repo's marketplace and ask
  "run the SEO optimizer on example.com for invoice software, time tracking".
- **JoyStream**: the catalog imports every skill in this plugin. The SEO Optimizer agent
  runs the modules as steps (Search Console and searches best effort), renders the PDF
  report into the run's files and posts a summary card. Only the delivery channel needs a
  connection. `website-seo-optimizer` describes that agent's shape; it is not a step
  itself.
- **SEO Watch** (on a schedule, e.g. every 3 days): the same modules, then
  `website-seo-changes` compares them with the last check's snapshot. The card shows only
  what is new, fixed, still open and overdue, rank moves and Core Web Vitals that crossed
  a line, and links the full PDF. A Google Sheet keeps the snapshot between runs.

## Settings you can change without code

Each module reads these from its inputs, and a missing or empty one falls back to the
default, so a team can tune a run without touching a script. On JoyStream, add the
setting to the agent's `input_schema` and bind the step's input to it, for example
`"max_pages": "input.max_pages"` on the `audit` step. The SEO Optimizer agent offers the
first, fourth and sixth.

| Setting | Module and input | Default | Range |
|---|---|---|---|
| Pages to audit | page audit `max_pages` | 15 | 1 to 30 |
| Internal links checked | page audit `max_link_checks` | 40 | up to 100 |
| Pages measured for speed | performance `max_pages` | 3 | 1 to 5 |
| Phone or desktop speed | performance `strategy` | `mobile` | `mobile`, `desktop` |
| Real-user data (Chrome UX Report) | performance `field_data` | `true` | `true`, `false` |
| Competitors compared per keyword | content gap `max_competitors` | 5 | 1 to 8 |
| Opportunities returned | opportunities `top_n` | 10 | up to 25 |
| Fewest impressions for a query | opportunities `min_impressions` | 50 | any |
| Words that mark a branded query | opportunities `brand_terms` | the host's first label (`example`) | a list |

## Changing what a module does

A new check, a different score or another chart is a change to the module's script, and
JoyStream takes skills from GitHub only: there is no file upload. To change one:

1. **Copy the module.** Fork this repository, or copy the module's folder (for example
   `skills/website-seo-page-audit`) into a repository of your own. From JoyStream,
   `jstm skill pull website-seo-page-audit --out ./my-copy` downloads the same folder.
2. **Rename it and edit.** Give the copy its own `name` (say `acme-seo-page-audit`) in
   its `SKILL.md`, and change the script path in its Reasoning Flow to match the new
   folder (`/workspace/skill/acme-seo-page-audit/scripts/audit.py`). Edit the script, and
   the output schema in `assets/` if the output changes: the later modules read the
   fields that are already there, so add new fields rather than renaming old ones. The
   scripts use Python's standard library only.
3. **Check it.** In this repository, run
   `uv run --with pytest --with pyyaml --with jsonschema pytest tests` and
   `uv run scripts/validate.py`, or run the script on its own with `--input` and an
   example from `assets/examples/`.
4. **Add it to JoyStream.** Under **Add skill repository**, add your repository and sync
   it (or `jstm skill repo add` then `jstm skill repo sync`).
5. **Point the agent at it.** In your copy of the SEO Optimizer, change the step's skill
   to `acme-seo-page-audit` and build the agent again.

The output file each script saves in the sandbox (`/tmp/website-seo-page-audit.json`,
which the content gap and report modules read) is named in the script, not by the skill,
so a renamed copy still hands its pages to the later steps.
