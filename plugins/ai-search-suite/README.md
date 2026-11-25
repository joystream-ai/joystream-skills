# ai-search-suite

The SEO Optimizer for any website. Start with **`website-seo-optimizer`**: it runs the
other five skills in order, skips any whose data is missing, and says what it skipped.

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
website-seo-brief           (ranked, copy-ready titles, metas, sections, links, JSON-LD)
```

| Skill | Kind | Needs |
|---|---|---|
| [`website-seo-optimizer`](./skills/website-seo-optimizer/SKILL.md) | task | Nothing itself: sequences the skills below |
| [`website-seo-page-audit`](./skills/website-seo-page-audit/SKILL.md) | sandbox | Outbound internet. Works on any public site, yours or a competitor's |
| [`website-seo-performance`](./skills/website-seo-performance/SKILL.md) | sandbox | Lighthouse and Chrome (both in the JoyStream sandbox); the free PageSpeed API for real-user data |
| [`website-seo-content-gap`](./skills/website-seo-content-gap/SKILL.md) | sandbox | Focus keywords and a web-search step per keyword (SerpApi, Tavily, Brave or Exa) |
| [`website-seo-opportunities`](./skills/website-seo-opportunities/SKILL.md) | sandbox | Google Search Console query × page rows for the last 28 days (and the 28 before) |
| [`website-seo-brief`](./skills/website-seo-brief/SKILL.md) | instruction | The audit, and the other outputs when there are any |

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
  runs the modules as steps (Search Console and searches best effort), then saves the
  brief as a file and posts a Slack card. `website-seo-optimizer` describes that agent's
  shape; it is not a step itself.
