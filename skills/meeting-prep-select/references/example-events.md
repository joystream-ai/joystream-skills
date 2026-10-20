# Calendar events behind the examples

The calendar events are evidence (the output of the agent's calendar step), not input
data, so they are not in the `*.input.json` files. They are summarized here so a reviewer
can check each expected output. All names, domains and ids are made up.

## hourly-window-mixed (Google Calendar `items[]`)

`now` 2026-10-20T13:00:00Z, window 60-120 minutes: `[14:00Z, 15:00Z)`. You are
`dana@ourco.example` (`self: true`).

| id | summary | start (UTC) | attendees and flags | result |
|---|---|---|---|---|
| `7kq2orbit01` | Orbit Ledger <> OurCo intro | 14:15 | you; `riley.sample@orbitledger.example` ("Riley Sample", accepted); `sam.example@orbitledger.example` (no display name, needsAction); a room (`resource: true`). Description: "Agenda: walk through their month-end close process and our pilot proposal. Join with Google Meet https://meet.google.com/abc-defg-hij" | accepted, slot 0 |
| `9zz1orbitcopy` | Orbit Ledger <> OurCo intro | 14:15 | same `iCalUID` as `7kq2orbit01` (a team calendar) | duplicate |
| `5stand0up` | Weekly standup | 14:30 | you and two `ourco.example` colleagues | internal_only |
| `2lumdemo` | Lumen Payroll demo | 14:00 | you (`responseStatus: declined`), one `lumenpayroll.example` attendee | declined |
| `8nwsync` | Vendor sync - Northwind | 14:45 | `status: cancelled` | cancelled |
| `4focus` | Focus time | 14:00 | `eventType: focusTime` | not_a_meeting |
| `6intv` | Candidate interview | 14:30 | you and a `gmail.com` attendee | skip_title |
| `3hv8exan22` | Discovery: Example Analytics | 14:50 | organizer `jordan@exampleanalytics.example` ("Jordan Placeholder", accepted); `casey@advisors.example` ("Casey Example", optional, tentative); you; one colleague. No description | accepted, slot 1 |
| `1acmepart` | Partner call - Acme Example | 16:00 | external | outside_window (counted only) |
| `0offsite` | Team offsite | all day (`start.date`) | | all_day (counted only) |

`Example Analytics` has one attendee, `advisors.example` one: the tie goes to the
organizer's domain.

## no-external-meetings (Google Calendar `items[]`)

`now` 2026-10-20T05:00:00Z, window 0-1440 minutes (a daily run). No `own_domains`; the
attendee with `self: true` is `dana@ourco.example`.

| id | summary | attendees and flags | result |
|---|---|---|---|
| `a1one2one` | Dana / Lee 1:1 | you, `lee@ourco.example` | internal_only |
| `b2gym` | Gym | no attendees | no_other_attendees |
| `c3review` | Pipeline review | you, three `ourco.example` colleagues | internal_only |
| `d4vendor` | Vendor pitch | you (declined), one `vendor.example` attendee | declined |
| `e5holiday` | Public holiday | all day | all_day (counted only) |
