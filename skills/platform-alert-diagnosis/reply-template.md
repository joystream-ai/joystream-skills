# Slack reply template

The `text` of the one `slack.reply_message` call. Slack mrkdwn, at most 1,500
characters in total. Fill the `{…}` slots; drop a line marked optional when its slot is
empty. Nothing else is added: no greeting, no extra sections, no links.

```
*Diagnosis* · `{rule}` · {env} · {timing_label}
*Cause:* {cause}
*Confidence:* {confidence}{unsure_suffix}
*Evidence:*
• {evidence_1}
• {evidence_2}
*Recommendation:* `{action}`{service_suffix} — {recommendation_text}
{stale_line}
_Read-only diagnosis by the ops agent. No action was taken._
```

## Slots

| Slot | Value |
|---|---|
| `{rule}` | `data.rule` |
| `{env}` | `data.env` |
| `{timing_label}` | `fired {HH:MM}Z` from `firing_since`; add ` ({N} min ago)` when the age is known |
| `{cause}` | `cause`, one sentence |
| `{confidence}` | `high`, `medium`, `low` or `unsure` |
| `{unsure_suffix}` | ` — {unsure_reason}` when confidence is `low` or `unsure`, else empty |
| `• {evidence_n}` | one bullet per evidence line, 1 to 5, each at most 200 characters, already redacted. With no evidence, one bullet: `• none: {why}` |
| `{action}` | `recommendation.action` |
| `{service_suffix}` | ` ({service})` when `recommendation.service` is set, else empty |
| `{recommendation_text}` | `recommendation.text`, one sentence addressed to a human |
| `{stale_line}` | optional. When `timing` is `stale`: `_Alert is over 2 hours old; the current state may have moved on._` When `unknown`: `_Alert age unknown._` |

## Rules

- Strip backticks, `*`, `_` and `~` from evidence lines and from `cause` before
  inserting them, so log text cannot change the formatting.
- Replace `<`, `>` and `&` in inserted text with `&lt;`, `&gt;` and `&amp;`, so log text
  cannot form a Slack link or mention (`<!channel>`, `<@U…>`, `<http…|…>`).
- If the reply is over 1,500 characters, shorten evidence lines first (to 120
  characters, then drop the weakest), never the recommendation.

## Example

```
*Diagnosis* · `dispatch_stale` · production · fired 10:04Z (3 min ago)
*Cause:* js-worker has crashed on start since the 10:02Z deploy.
*Confidence:* high
*Evidence:*
• 10:02:41Z js-worker: ImportError: cannot import name 'run_claim' from 'services'
• 10:03:12Z js-worker: ImportError: cannot import name 'run_claim' from 'services'
• get_service_instance: js-worker latest deployment CRASHED, 1 replica
*Recommendation:* `rollback_recent_deploy` (js-worker) — Roll js-worker back to the deploy before 10:02Z, then fix the import.
_Read-only diagnosis by the ops agent. No action was taken._
```
