# ai-search-suite

SEO for any website, plus Search Console click estimates when it is connected. Three
skills that chain into one SEO agent:

```
website-seo-opportunities  (Search Console rows → ranked opportunities, extra clicks)   optional
            │ target_urls
            ▼
website-seo-page-audit      (any domain: robots, sitemap, up to 30 pages of raw HTML)   always
            │ findings, pages
            ▼
website-seo-brief           (ranked, copy-ready titles, metas, sections, links, JSON-LD)
```

| Skill | Kind | Needs |
|---|---|---|
| [`website-seo-page-audit`](./skills/website-seo-page-audit/SKILL.md) | sandbox | Outbound internet. Nothing else: works on any public site, yours or a competitor's |
| [`website-seo-opportunities`](./skills/website-seo-opportunities/SKILL.md) | sandbox | Google Search Console query × page rows for the last 28 days (and the 28 before) |
| [`website-seo-brief`](./skills/website-seo-brief/SKILL.md) | instruction | The audit, and the opportunities when there are any |

## What is skipped when data is missing

| Missing | What happens |
|---|---|
| Search Console (not connected, or no access to the property) | No opportunities step. The brief runs in `on_page` mode, with no click estimates, and says so |
| A page that errors, or no sitemap | Reported as a finding; the audit carries on with the pages it can read |
| The homepage | The audit returns `outcome: error`; the brief says the site could not be read instead of guessing |
| Keyword volumes, rankings, backlinks | Not part of v1: they need an SEO data provider (Semrush, Ahrefs, DataForSEO) |

## Use

- **Claude Code / claude.ai**: install the plugin from this repo's marketplace and ask
  "audit the SEO of example.com and write the fixes".
- **JoyStream**: the catalog imports the three skills from this plugin. The SEO Optimizer
  agent runs them as steps, with the Search Console steps marked best effort, then saves
  the brief as a file and posts a Slack card.
