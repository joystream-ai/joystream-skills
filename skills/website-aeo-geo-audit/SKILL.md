---
name: website-aeo-geo-audit
description: Score how ready a website is to be found, understood and cited by AI answer engines (ChatGPT, Claude, Perplexity, Gemini, Google AI Overviews) — an AEO/GEO audit across six categories with strengths, gaps, prioritized fixes, a full markdown report and a short summary ready to post. Optionally checks a list of keywords the site should answer (point it at a competitor's site to see which keywords they cover) and compares with the previous audit's saved state to say what changed. Judges the pages and search results the agent already fetched, not every URL on the domain; it fetches nothing, stores nothing and posts nowhere.
license: MIT
metadata:
  author: joystream
  version: "1.2.0"
  category: marketing
  joystream-skill-kind: instruction
  joystream-input-schema: assets/input.schema.json
  joystream-output-schema: assets/output.schema.json
  gtm-issue: "1076"
---

# Purpose

Tell a site owner how likely AI answer engines are to find their site, understand
what the brand is, and cite it — and what to fix first. Answer engines assemble
replies from pages they are allowed to crawl, can read without running JavaScript,
can quote in a sentence, and see corroborated by other sources. This skill scores
each of those conditions from evidence and turns the gaps into a ranked to-do list.

It is a **judgment** skill. The agent that runs it does the fetching (a page
extraction step and a web search step) and the delivery (posting, emailing, filing).
This skill reads what those steps returned and produces the audit. It never fetches,
never writes files, and never runs code.

This skill performs no external operations; the agent's page-extraction step supplies
`pages` and its web-search step supplies `search_results`, and a later step delivers
`summary_markdown` or `report`.

**Scope.** The audit covers the pages in `pages`: normally the homepage, the three site
files and two to five key pages. It is not a crawl of every URL under the domain, and
the report says which pages it read. To audit more pages, the agent extracts more.

**Competitors.** The same skill audits any site. To see how a competitor speaks to a
set of keywords, the agent runs it with the competitor's `website_url` and the same
`keywords`.

# Inputs

- `website_url` — the normalized site. Every page and source is judged relative to
  its host.
- `pages` — every URL the extraction step attempted, successes **and** failures. A
  missing `/llms.txt` is a finding; a missing entry for it is a gap in the evidence.
  Tell them apart.
- `search_results` — the queries the search step ran and what came back.
- `brand_description` — optional; the brand's own one-paragraph self-description. When
  present it is the yardstick for step 7: coverage that describes a different product is
  another entity, not this brand's authority.
- `keywords` — optional, up to 20 terms or questions the site should answer (e.g.
  `agent governance`, `what is an AI agent platform`). Each gets a coverage verdict in
  step 9. The agent should also run one search per keyword so step 9 can check whether
  the site shows up for it.
- `brand_name`, `audit_date`, `summary_max_chars` — optional presentation inputs.

The previous audit's saved state is not an input field. When the agent keeps state
between runs, a read step before this one returns the last run's `state_out` (from a
Sheet, a Notion page or a memory connector), and a write step after it saves the new
`state_out`. This skill never stores anything.

Target sites, channels, schedules and report destinations belong to the agent, never
to this skill.

# Reasoning Flow

1. **Classify the site files.** For `/robots.txt`, `/llms.txt` and `/sitemap.xml`,
   record `present` (content returned), `missing` (the attempt failed with a not-found
   style error or returned an error page), or `unreachable` (not attempted, or failed
   for another reason). Only `present` content is evidence. A present sitemap counts
   in full only when it lists the key pages; a redirect to another sitemap file (for
   example `wp-sitemap.xml`) that the extraction followed is still present.
2. **Detect client rendering.** If the homepage content is empty, a near-empty shell,
   or a "please enable JavaScript" notice, set `client_rendered: true`. This is a
   headline gap on its own: most AI crawlers do not execute JavaScript, so they see
   what the extraction step saw.
3. **Crawler access.** From robots.txt, work out whether the major AI user agents may
   crawl the site — GPTBot, OAI-SearchBot, ChatGPT-User, ClaudeBot, anthropic-ai,
   PerplexityBot, Google-Extended, Applebot-Extended, CCBot. Note a declared sitemap.
   Quote the exact rule lines you relied on.
4. **llms.txt.** If present, check it reads as an index for models: the site name as
   a heading, sections, links to key pages each with a real description. Presence
   alone is uncommon and counts in the site's favour.
5. **Answerability**, across the homepage and key pages. Look for: a single sentence
   near the top that says what the brand or product *is*, which a model could quote;
   headings phrased as the questions people ask; short self-contained answer
   paragraphs under those headings; and an FAQ, counting real question-and-answer
   pairs (fewer than five is thin). Quote the definitional sentence if one exists.
6. **Metadata.** From what the extracted text exposes — titles, descriptions, social
   card fields, canonical and robots meta where the extraction includes them. Check
   each page, not just the homepage: name every page missing a description, and flag
   a social card type that does not fit the page (`article` on the homepage, `website`
   on a blog post) and any `noindex`. Rendered text rarely carries JSON-LD, so
   structured data is `unverified` unless the content shows it; say that rather than
   scoring it as absent.
7. **External authority.** From `search_results`: count distinct third-party domains
   that discuss the brand (exclude the brand's own domains and obvious reposts of its
   press releases). A result counts only if it describes the brand the site and
   `brand_description` describe; results about another product with the same or a
   similar name are excluded and noted as a collision gap. Check whether the provider's
   generated answer describes the brand the way the site does, and whether the site
   ranks for its own definitional query. Quote any mismatch.
8. **Depth and freshness.** Dated, authored content; more than a launch post; pages
   that answer adjacent questions.
9. **Keywords** (only when `keywords` is given). For each keyword, in the order given:
   - `on_site`: `answered` when a page in `pages` has a heading or a self-contained
     paragraph that answers it (quote it); `mentioned` when the term appears without an
     answer; `absent` when no readable page uses it. If the site could not be read, use
     `unverified`.
   - `in_search`: from the search the agent ran for that keyword (match on the query
     text): `ranks` when a result is on the site's host, `not_ranking` when the search
     ran and no result is, `not_searched` when no query matches the keyword.
   - `note`: one line, and the source ids it rests on.
   A keyword that is `absent` on site is a gap; three or more `absent` keywords make the
   top keyword gap one of the five `gaps`, and each gets a recommendation naming the
   page that should answer it.
10. **Score** each category 0–10 with the rubric below. `overall_score` = mean × 10,
    rounded. Keywords inform `answerability` and `external_authority` but are not a
    seventh category. Set each category's `confidence`: `verified` when the evidence
    was present, `partial` when some was missing, `unverified` when the score rests on
    absence of evidence.
11. **Compare with the last run** (only when a prior step returned state with an entry
    whose `item_key` is `site:<host>`). Compare its `data` with this run: the overall
    score, each category score, each site file's status and each keyword's `on_site`.
    Write one line per change in `changes_since_last_run`, e.g. `llms.txt: missing →
    present` or `answerability: 5 → 7`, newest facts only, at most 10. No change at all
    is one line: `No change since <observed_at>.` When there is no previous entry, leave
    `changes_since_last_run` out.
12. **Write the findings.** Up to five strengths and five gaps, each with its
    evidence. Recommendations ranked by impact, each naming the page, file or field to
    change, with `effort`.
13. **Write `state_out`** when `audit_date` is given: one entry with `item_key`
    `site:<host>` (lower case, no `www.`), `observed_at` = `audit_date`, `fingerprint`
    = `score=<overall>;files=<robots>/<llms>/<sitemap>` using `p`, `m` or `u` for each
    status, and `data` = `{overall_score, categories: {<key>: score}, site_files,
    keywords: {<keyword>: on_site}}`. Without `audit_date`, leave `state_out` out.
14. **Render** `report_markdown` in the report shape, then `summary_markdown` — the
    score, verdict, top three strengths, top three gaps, one line on keyword coverage
    and one on what changed when those apply, and one line on where the full report is
    — trimmed to `summary_max_chars`, never cut mid-line.

# Scoring rubric

| Category | 0–2 | 3–5 | 6–8 | 9–10 |
|---|---|---|---|---|
| `crawler_access` | robots.txt blocks most AI agents, or the site is client-rendered with nothing readable | some AI agents blocked, or key pages marked noindex | readable and allowed, with friction (no sitemap declared, sections disallowed) | AI agents allowed, sitemap declared and reachable, content readable without JavaScript |
| `llms_txt` | absent on an otherwise hard-to-read site | absent on a well-structured site (cap 3), or present but malformed/stale | present and well-formed; thin descriptions or missing key pages | present, well-formed, complete, current |
| `answerability` | nothing a model could quote | marketing copy without a definition; no FAQ | a clear definition; some question headings; thin FAQ | quotable definition up top, question headings, five-plus FAQ pairs, self-contained answers |
| `metadata` | titles/descriptions missing or duplicated site-wide | inconsistent across pages | good basics with specific gaps | complete on every page audited, canonical present, structured data shown |
| `external_authority` | no independent coverage | coverage only on owned or syndicated channels | a few independent sources; answers mostly accurate | six-plus independent sources; generated answers accurate; site ranks for its definition |
| `depth_freshness` | stale or near-empty | launch-era only; undated | solid core pages; some recent dated content | steady dated, authored publishing covering adjacent questions |

Score conservatively where evidence is partial, and say so in `caveats`. Do not
inflate a score to be kind, and do not zero a category for one fixable gap.

# Output

`report_markdown` follows this shape:

```markdown
# AEO/GEO audit: <host>
<audit_date, if given> · Overall **<overall_score>/100** — <verdict>

| Category | Score | Finding |
|---|---|---|
| AI crawler access | n/10 | … |
| llms.txt | n/10 | … |
| Answerability | n/10 | … |
| Metadata | n/10 | … |
| External authority | n/10 | … |
| Depth & freshness | n/10 | … |

## Since last run        (only when changes_since_last_run is present)
## Strengths
## Gaps
## Keywords              (only when keywords were given: keyword | on site | in search | note)
## Fix first (ranked)
## What this audit could not verify
## Pages audited         (every url in pages, with present / missing / unreachable)
## Sources
```

`summary_markdown`, for example:

```
*AEO/GEO audit — example.com: 62/100*
Readable and crawlable, but hard for AI engines to quote.
Strengths: AI crawlers allowed · llms.txt present · clear product definition
Gaps: no FAQ · thin third-party coverage · metadata unverified
Full report available from the run.
```

`report` is the same audit as the channel-neutral report, for agents that render it
with `channel-message-render` instead of posting `summary_markdown`:

- `title`: `AEO/GEO audit: <host>`. `summary`: the overall score and the verdict, in
  plain text. `severity`: `attention` when `overall_score` is below 50 or
  `client_rendered` is true, else `info`.
- `sections`, in this order: `Since last run` (only when `changes_since_last_run` is
  present; one item per line, no citations, `label` `change`), `Scores` (one item per
  category, text `<Category>: n/10 — <summary>`, `label` the confidence), `Strengths`,
  `Gaps`, `Keywords` (only when keywords were given; one item per keyword, text
  `<keyword>: <note>`, `label` the `on_site` value), `Fix first` (one item per
  recommendation, in priority order, `label` the effort). Every item that states a
  finding cites the sources it rests on; a recommendation that makes no factual claim
  has empty `citations` and a `label`.
- `sources`: the entries of the top-level `sources` field, each with an id `S1`, `S2`,
  … in the same order, the url exactly as it appears in `pages` or `search_results`,
  its title, and `provider` when known. List the site pages you relied on there too,
  with `kind: site`.
- `unknowns`: the caveats and everything listed under "What this audit could not
  verify".
- `meta`: `skill: website-aeo-geo-audit`, `skill_version: 1.2.0` and `generated_at` =
  `audit_date`. Leave `meta` out when `audit_date` is absent; never guess a date.

`keyword_coverage` (one entry per keyword, in input order), `changes_since_last_run`
and `state_out` are described in steps 9, 11 and 13.

# Constraints

- **Evidence or nothing.** Every strength, gap and score traces to a page, a robots
  line, or a search result in the inputs. Quote the real text. Never describe a page
  that is not in `pages`.
- **Absence of evidence is not evidence of absence.** A URL that was never attempted,
  or failed for a non-not-found reason, is `unreachable` and lowers `confidence`, not
  the score.
- **No fetching, no files, no code.** If more evidence is needed, name it in `caveats`
  so the agent can fetch it next time.
- **Cite every third-party source** used, in `sources`.
- **The summary stands alone** and never exceeds `summary_max_chars`.

# Edge Cases

## Client-rendered site
Homepage empty or JavaScript-gated: `client_rendered: true`; `crawler_access` at most
2; lead the gaps and the recommendations with server-side rendering or prerendering of
key pages. Still score external authority from `search_results`.

## Extraction failed for everything
All `pages` failed: score the on-site categories `unverified` at conservative values,
score `external_authority` normally, and make the first caveat say the site could not
be read — the audit is mostly an external-visibility audit.

## No search results
Empty `search_results` or empty `results`: `external_authority` gets `unverified` and
at most 3; say whether it looks like no coverage or no search was run.

## Brand name collision
Results about a different entity with the same name: exclude them, and note the
collision as a gap — answer engines will conflate the two.

## Keywords but no keyword searches
Judge `on_site` normally, set every `in_search` to `not_searched`, and add a caveat
that the agent should run one search per keyword.

## Previous state for another site
A previous entry whose `item_key` is not `site:<host>` for this `website_url` is
ignored; treat the run as a first audit.

# Examples

## Strong site, weak corroboration
**Input:** homepage with a clear one-line definition, robots.txt allowing all agents
with a sitemap, llms.txt present, a nine-question FAQ; search returns only the brand's
own pages.
**Output:** `crawler_access` 9, `llms_txt` 8, `answerability` 9, `metadata` 7
(`partial`), `external_authority` 3, `depth_freshness` 6 → `overall_score` 70. Top gap
and first recommendation: earn independent coverage; the site is ready to be cited, but
nothing else corroborates it.

## JavaScript-only site
**Input:** homepage extraction returns an empty shell; robots.txt and sitemap
unreachable; search finds an app-store listing and two directories.
**Output:** `client_rendered: true`, `overall_score` around 20, first recommendation
server-side rendering of the homepage and key pages, caveat that on-site categories
are unverified.

## Competitor site with keywords
**Input:** a competitor's site, three keywords and one search per keyword; robots.txt
blocks GPTBot, llms.txt returns 404.
**Output:** `keyword_coverage` answered / mentioned / absent, the absent keyword as a
gap with a recommendation, and `state_out` for the next run to compare against. The
paired cases are in `assets/examples/`.
