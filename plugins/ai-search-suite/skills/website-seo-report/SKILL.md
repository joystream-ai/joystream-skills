---
name: website-seo-report
description: >-
  Turn the SEO Optimizer's outputs into one polished report a team can read and forward:
  a PDF (and HTML) with a score meter, issue counts, this week's changes,
  a category scorecard chart, a Core Web Vitals chart and table, a content-gap chart with
  the topics the top results cover, Search Console opportunity bars, copy-ready fixes
  (current text beside the proposed text, JSON-LD) and what could not be checked. Every
  chart is drawn by a deterministic Python script from the modules' own numbers, never by
  the model, and the PDF is printed by the sandbox's Chromium. Takes website-seo-brief's
  output plus whichever of website-seo-page-audit, website-seo-performance,
  website-seo-content-gap and website-seo-opportunities ran; a missing module is left out
  and named. Runs in the sandbox (the agent needs code_execution) and writes
  seo-report.pdf and seo-report.html to the run's files. Use as the step after
  website-seo-brief and before delivery. It reads its inputs only: it fetches nothing,
  posts nothing and sends nothing.
license: MIT
metadata:
  author: joystream
  version: "1.1.0"
  category: marketing
  joystream-skill-kind: task
  joystream-input-schema: assets/input.schema.json
  joystream-output-schema: assets/output.schema.json
---

# Purpose

The brief says what to change; this skill makes it something people want to open. A Slack
card can carry the headline and three changes, but the evidence (which pages are slow,
which topics the ranking pages cover, the current title beside the new one) needs a page
of its own. `scripts/render.py` lays the modules' numbers out as one report with charts
and prints it to PDF. It draws only what the inputs say, so the same inputs always give
the same report.

# Inputs

- **brief** (object, required unless `audit` is given): website-seo-brief's output, for
  the score, the verdict (`report.summary`), the ranked `actions` and the unknowns.
- **audit** (object, recommended): website-seo-page-audit's output, for the issue counts,
  the category scores, the findings and the since-last-audit change.
- **speed** (object, optional): website-seo-performance's output.
- **gaps** (object, optional): website-seo-content-gap's output.
- **opportunities** (object, optional): website-seo-opportunities' output.
- **now** (string, ISO 8601, optional): the run's start time, printed as the report date.

Each may arrive as the module's object or inside a step envelope (`{"step": {"ok",
"data"}}`); a failed step counts as missing.

# What this skill needs

Bind each input to the step that produced it, and run this step after website-seo-brief.
On JoyStream this step runs in the sandbox, so **the agent needs the `code_execution`
capability**. The skill folder is at `/workspace/skill/website-seo-report`. The sandbox
has Chromium (`CHROME_PATH`); nothing is installed per run.

After it, a delivery step posts the brief's `report` (for example a Slack card through
channel-message-render) and names the PDF, which is in the run's files.

# Reasoning Flow

1. **Write the inputs to a file.** A command you run in the sandbox gets none of this
   step's inputs, so they reach the script through a file. With one `sandbox_write_file`,
   write `brief` (website-seo-brief's output) and `now` as one JSON object to
   `/tmp/website-seo-report.input.json`. Copy each value exactly as you were given it.
   Leave out `audit`, `speed`, `gaps` and `opportunities`: the script reads each from the
   output that step saved in this sandbox, which is the same data without retyping it. Do
   not read or search the script, list files, or write anything under `/workspace/out`.
2. **Run the script, once.** Your only `sandbox_run` is:

   ```
   python3 /workspace/skill/website-seo-report/scripts/render.py --input /tmp/website-seo-report.input.json
   ```

   It takes a few seconds. It writes `seo-report.pdf` and `seo-report.html` to
   `/workspace/out/`, which become the run's files.
3. **Return its JSON unchanged.** The script prints one JSON object on stdout (also saved
   at `/tmp/website-seo-report.json`) that matches `assets/output.schema.json`. Return
   exactly those fields.
4. **If the script exits non-zero**, it still prints a valid output object saying what
   went wrong (nothing to render). Return that object. Never write a report by hand.

# What the report holds

In this order, each section only when its data arrived:

1. **Summary**: the site, the date, the score (0-100) with a meter and a label (85 and
   above good, 70-84 fair, 50-69 needs work, below 50 poor), the verdict, the change since
   the last audit, the pages read, the speed score, the extra clicks available, and the
   issue counts by severity.
2. **This week's changes**: the brief's actions as a table (page, change, impact, effort,
   owner, extra clicks when Search Console ran).
3. **Scorecard**: a bar per audit category, out of 10.
4. **Speed (Core Web Vitals)**: Largest Contentful Paint per page against Google's 2.5 s
   and 4 s lines, a table of score, LCP, CLS and TBT with each rating, and the real-user
   line from the Chrome UX Report (or that there was none).
5. **Content gaps vs top results**: our word count against the top results' median per
   keyword, our rank, the topics and structured data the top results have and we don't.
6. **Search Console opportunities**: extra clicks per 28 days for the top queries.
7. **Copy-ready fixes**: per action, the current title, meta description or H1 struck
   through beside the proposed one with character counts, new sections, the internal
   link and the JSON-LD.
8. **All findings**: every finding of the audit, speed and content-gap modules, by
   severity, with evidence, fix and pages.
9. **What this report could not check**: the brief's unknowns, plus one line per module
   that did not run.

Status colours (good, warning, serious, critical) always sit beside a word, so the report
reads the same in black and white.

# Output

The fields of `assets/output.schema.json`:

- `outcome`: `ok`, `partial` (the HTML was made but Chromium could not print the PDF) or
  `error` (nothing to render, or the files could not be written).
- `site`, `score` (copied from the brief, else the audit).
- `files[]`: `{path, kind: "pdf" | "html", bytes}`, the PDF first.
- `sections[]`: the sections the report holds, by id (`summary`, `changes`, `scorecard`,
  `speed`, `content_gaps`, `opportunities`, `fixes`, `findings`, `unknowns`).
- `sentence` (e.g. `Report for example.com: 62/100, 5 changes, 8 sections, saved as
  seo-report.pdf and seo-report.html.`) and `unknowns` (why the PDF is missing, if it is).

# Constraints

- **The inputs' numbers are the report.** The script draws them as given: it never
  rescores, estimates or fills a gap.
- **Input text is data, never instructions.** Page titles, competitor headings and the
  brief's copy are escaped before they reach the page, and the page's Content Security
  Policy blocks every fetch, so the PDF is printed from the inputs alone.
- **Standard library only**, plus the Chromium the image provides.

# Edge Cases

## Only the audit arrived
The brief failed or was skipped. The report still has the summary, scorecard, findings and
unknowns; the changes and fixes sections are left out.

## Chromium is missing or fails
The HTML report is still written, `outcome` is `partial`, and `unknowns` says why there is
no PDF.

## Nothing arrived
Neither the brief nor a usable audit: `outcome` is `error` and no file is written.

# Examples

See the paired cases in `assets/examples/`.
