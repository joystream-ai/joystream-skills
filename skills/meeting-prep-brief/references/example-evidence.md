# Evidence behind the examples

The evidence is the output of the agent's earlier steps, not input data, so it is not in
the `*.input.json` files. It is summarized here so a reviewer can check each expected
output. All names, domains, ids and urls are made up.

## full-brief-with-history

Meeting: the `7kq2orbit01` accepted entry from `meeting-prep-select`'s
`hourly-window-mixed` example. `now` 2026-10-20T13:05:00Z, `lookback_days` 90.

| Step | Item | Used as |
|---|---|---|
| web search (company) | `https://orbitledger.example/`: "billing and invoicing software for mid-size wholesalers" | S1 |
| web search (company) | `https://news.example.com/2026/09/15/orbit-ledger-raises-series-a`, published 2026-09-15: "$12M Series A" | S2 |
| web search (company) | `https://orbitledger-astro.example/`: "Orbit Ledger Ltd, telescopes and star charts, Leeds" | rejected (another company) |
| web search (company) | `https://orbitledger.example/careers/financial-systems-analyst`, no date | undated, dropped |
| web search (people) | `https://orbitledger.example/about/team`: "Riley Sample, Controller", "Sam Example, Head of Finance Operations" | S3 |
| web search (people) | `https://blog.example.org/riley-sample-close-in-five-days`, 2026-08-30, byline "Riley Sample, Controller at Orbit Ledger" | S4 |
| web search (people) | `https://food.example.net/sam-example-chef`: "Sam Example, head chef at Example Bistro" | rejected (another person) |
| email search | Gmail message `18f2a9c0d1`, 2026-10-08, from an `orbitledger.example` address: Riley asks for pilot pricing for 40 users and ERP integration; the message also holds Riley's phone number (not copied) | S5 |
| CRM lookup | Attio record `0b7e5c2a`, deal "Orbit Ledger pilot", stage "Proposal sent", updated 2026-10-12 | S6 |

## attendee-not-confirmed

Meeting: the `3hv8exan22` accepted entry from the same select example. No email or CRM
step in the agent.

| Step | Item | Used as |
|---|---|---|
| web search (company) | `https://exampleanalytics.example/`: "product analytics for mobile game studios" | S1 |
| web search (company) | a press release dated 2025-11-03 | older than 90 days, dropped |
| web search (people) | three pages about "Jordan Placeholder", a basketball player | rejected |
| web search (people) | one page about "Jordan Placeholder, VP Marketing at Northwind Example" | rejected |
| web search (people) | nothing about Casey Example | |

## empty-slot

`item` is `null`: no evidence is read.
