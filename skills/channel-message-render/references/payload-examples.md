# Payload examples

Optional depth for `channel-message-render`. The rules are in SKILL.md; this file only
shows them applied. All data is made up.

## The report the brief examples render

`slack-brief`, `discord-brief` and `markdown-brief` in `assets/examples/` render this
report, the full output of the task-skill step this one depends on:

```json
{
  "title": "Orbit Ledger — account brief",
  "summary": "A finance-operations SaaS for mid-market teams that raised a Series A in September and is hiring finance engineers.",
  "severity": "info",
  "sections": [
    {
      "heading": "What they do",
      "items": [
        {
          "text": "Sells month-end close automation to mid-market finance teams.",
          "citations": [
            "S1"
          ],
          "confidence": "high"
        },
        {
          "text": "Plans start at $300 per month.",
          "citations": [
            "S2"
          ],
          "confidence": "high"
        }
      ]
    },
    {
      "heading": "Recent events",
      "items": [
        {
          "text": "Raised a $12M Series A on 2026-09-22.",
          "citations": [
            "S3"
          ],
          "confidence": "high"
        },
        {
          "text": "Head of Finance is Jo Rivera, per the enrichment record.",
          "citations": [
            "S4"
          ],
          "confidence": "medium"
        }
      ]
    },
    {
      "heading": "Suggested next step",
      "items": [
        {
          "text": "Open with the month-end close timeline they describe on their blog.",
          "citations": [],
          "label": "recommendation"
        }
      ]
    }
  ],
  "sources": [
    {
      "id": "S1",
      "url": "https://orbit.example/",
      "title": "Orbit Ledger — close the books faster",
      "provider": "tavily"
    },
    {
      "id": "S2",
      "url": "https://orbit.example/pricing",
      "title": "Pricing",
      "provider": "tavily"
    },
    {
      "id": "S3",
      "url": "https://news.example.org/orbit-ledger-series-a",
      "title": "Orbit Ledger raises $12M",
      "provider": "exa"
    },
    {
      "id": "S4",
      "record_ref": "apollo:person/9b4d",
      "title": "Jo Rivera",
      "provider": "apollo"
    }
  ],
  "unknowns": [
    "Customer count: no public figure found."
  ],
  "footer": "Draft — not sent",
  "meta": {
    "skill": "account-research-brief",
    "skill_version": "1.0.0",
    "generated_at": "2026-10-12T09:00:00Z"
  }
}
```

`teams-action-needed` renders the same report with `severity: action_needed` and a new
summary; `email-empty-report` renders a report with no sections and one unknowns line;
`slack-injected-mentions` renders a report whose text carries `@channel`, `@here`,
`<!channel>` and a user mention.

## Slack

Header, summary, one section per report section (heading in bold, one bullet per item,
citations linked), unknowns, sources in a context block, footer last:

```json
[
  {
    "type": "header",
    "text": {
      "type": "plain_text",
      "text": "Orbit Ledger — account brief",
      "emoji": true
    }
  },
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "A finance-operations SaaS for mid-market teams that raised a Series A in September and is hiring finance engineers."
    }
  },
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*What they do*\n• Sells month-end close automation to mid-market finance teams. (<https://orbit.example/|S1>)\n• Plans start at $300 per month. (<https://orbit.example/pricing|S2>)"
    }
  },
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Recent events*\n• Raised a $12M Series A on 2026-09-22. (<https://news.example.org/orbit-ledger-series-a|S3>)\n• Head of Finance is Jo Rivera, per the enrichment record. (S4)"
    }
  },
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Suggested next step*\n• Open with the month-end close timeline they describe on their blog."
    }
  },
  {
    "type": "section",
    "text": {
      "type": "mrkdwn",
      "text": "*Unknowns*\n• Customer count: no public figure found."
    }
  },
  {
    "type": "context",
    "elements": [
      {
        "type": "mrkdwn",
        "text": "<https://orbit.example/|S1: Orbit Ledger — close the books faster> · <https://orbit.example/pricing|S2: Pricing> · <https://news.example.org/orbit-ledger-series-a|S3: Orbit Ledger raises $12M> · S4: Jo Rivera (Apollo record)"
      }
    ]
  },
  {
    "type": "context",
    "elements": [
      {
        "type": "mrkdwn",
        "text": "Draft — not sent"
      }
    ]
  }
]
```

## Discord

```json
{
  "content": "**Orbit Ledger — account brief**\nA finance-operations SaaS for mid-market teams that raised a Series A in September and is hiring finance engineers.",
  "embeds": [
    {
      "title": "What they do",
      "description": "• Sells month-end close automation to mid-market finance teams. ([S1](https://orbit.example/))\n• Plans start at $300 per month. ([S2](https://orbit.example/pricing))",
      "color": 1940433
    },
    {
      "title": "Recent events",
      "description": "• Raised a $12M Series A on 2026-09-22. ([S3](https://news.example.org/orbit-ledger-series-a))\n• Head of Finance is Jo Rivera, per the enrichment record. (S4)",
      "color": 1940433
    },
    {
      "title": "Suggested next step",
      "description": "• Open with the month-end close timeline they describe on their blog.",
      "color": 1940433
    },
    {
      "title": "Unknowns",
      "description": "• Customer count: no public figure found.",
      "color": 1940433
    },
    {
      "title": "Sources",
      "description": "[S1: Orbit Ledger — close the books faster](https://orbit.example/)\n[S2: Pricing](https://orbit.example/pricing)\n[S3: Orbit Ledger raises $12M](https://news.example.org/orbit-ledger-series-a)\nS4: Jo Rivera (Apollo record)",
      "color": 1940433,
      "footer": {
        "text": "Draft — not sent"
      }
    }
  ]
}
```

## Truncation

A Slack report with 4 sections of 25 items (100 lines) passes the 40-line limit. The first
section is shown whole, the second up to its 15th item, and the rest is one count line.
The fallback `text` says so:

```json
{
  "text": ":warning: Attention: Orbit Ledger — full review — A long review. Showing 40 of 100 items; …and 60 more not shown.",
  "last_blocks": [
    {
      "type": "context",
      "elements": [
        {
          "type": "mrkdwn",
          "text": "…and 60 more"
        }
      ]
    },
    {
      "type": "context",
      "elements": [
        {
          "type": "mrkdwn",
          "text": "<https://orbit.example/|S1: Orbit Ledger>"
        }
      ]
    }
  ],
  "items_total": 100,
  "items_shown": 40
}
```
