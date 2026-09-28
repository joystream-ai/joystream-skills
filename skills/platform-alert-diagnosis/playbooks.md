# Playbooks

One section per js-monitor rule. Each gives the services to check, the Railway reads
(in order, within SKILL.md step 4's limits), what normal and bad look like, and the
likely causes with their recommendation. "Logs" means `railway.get_environment_logs`
with `filter: "@service:<serviceId> @level:ERROR"` over the log window. "Deploy logs"
means `railway.get_deployment_logs` for one deployment id with `filter: "@level:ERROR"`.

Every rule starts the same way:

- If `data.recent_deploy` is set, or `snapshot.deploys.<service>.finished_at` is within
  15 minutes before `firing_since`, read that deploy's logs first. A deploy just before
  the alert is the most likely cause.
- The snapshot is the first sample. Quote it; do not re-derive it.
- A `rollback_recent_deploy` recommendation names the service and the Railway
  deployment id to roll back from. Railway deployment and service ids stay in the
  reply; every other UUID, such as a customer agent id in `queue.top_agents`, is
  redacted (SKILL.md Constraints).

Snapshot resource fields referenced below (ages in seconds):

- `queue`: `queued` (claimable), `queued_total`, `oldest_queued_age_s`, `top_agents`
  (`[{agent_id, queued}]`, up to 10).
- `relay`: `undelivered`, `oldest_undelivered_age_s`, `dead_letters_15m`.
- `builds`: `queued`, `oldest_queued_age_s`, `failed_15m`.
- `failures`: `platform_class` (platform-class failed runs in 15 minutes), `by_kind`.

## `backend_unhealthy`

js-backend's `/health` did not answer 200. Critical. Value 1, threshold 0.

- **Services:** js-backend.
- **Reads:** deploy logs for the recent js-backend deploy if any; `get_service_instance`
  js-backend; logs js-backend; `get_service_metrics` js-backend.
- **Normal:** `probes.backend.status` 200; latest deployment `SUCCESS`; memory well
  under its limit; no repeated ERROR line.
- **Bad:** `probes.backend.status` null (unreachable) or 5xx; latest deployment
  `CRASHED`, `FAILED` or still deploying; memory at its limit; the same error repeating.
- **Likely causes → recommendation:**
  - Errors start right after a js-backend deploy → `rollback_recent_deploy` js-backend.
  - Crashed or out of memory with no deploy → `restart_service` js-backend (high
    memory that keeps growing → `code_change` as well, named in `text`).
  - Database connection errors in the logs, or `resources.database: unavailable` →
    `investigate` the database (not a Railway action here).
  - CPU at its limit with request timeouts → `scale_backend`.
  - Nothing in the logs → `investigate`, confidence `unsure`.

## `dispatch_stale`

js-worker's `/health` reports dispatch `stale`, or the worker is unreachable. Critical.
Value 1, threshold 0.

- **Services:** js-worker.
- **Reads:** deploy logs for the recent js-worker deploy if any; `get_service_instance`
  js-worker; logs js-worker; `get_service_metrics` js-worker.
- **Normal:** `probes.worker.status` 200 and `dispatch` not `stale`; one healthy
  replica; no repeated ERROR line.
- **Bad:** `probes.worker.status` null or not 200/503; `dispatch: stale` (the dispatch
  loop stopped claiming); latest deployment `CRASHED` or `FAILED`; memory at its limit.
- **Likely causes → recommendation:**
  - Crash loop after a js-worker deploy → `rollback_recent_deploy` js-worker.
  - Out of memory, or the loop hung with no errors → `restart_service` js-worker.
  - Database errors in the loop → `investigate` the database.
  - Never `WEB_CONCURRENCY` above 1 on js-worker.

## `queue_backlog`

The oldest claimable queued run is older than the threshold (default 300 s). Warning.

- **Services:** js-worker.
- **Reads:** `get_service_instance` js-worker (replicas, latest deploy); logs js-worker;
  `get_service_metrics` js-worker.
- **Normal:** `queue.oldest_queued_age_s` under the threshold; `queued` spread over
  many agents; worker CPU and memory under their limits.
- **Bad:** one entry in `top_agents` holds most of `queued`; or the backlog is spread
  over many agents while the worker is busy; or `snapshot.rules.dispatch_stale` is also
  `firing`.
- **Likely causes → recommendation:**
  - One agent floods the queue → `pause_binding` (text: "the agent with the most queued
    runs in the alert snapshot"; never its id).
  - Spread-out load, worker healthy but at CPU limit → `scale_worker`.
  - The worker is down or stale → the `dispatch_stale` causes apply; recommend for that.
  - Concurrency limit reached with idle CPU (`max_in_flight` is a code constant) →
    `code_change`.

## `run_failure_rate`

More platform-class run failures in 15 minutes than the threshold (default 5). Warning.

- **Services:** follow the leading kind in `failures.by_kind`:
  - `dead_letter`, `stalled`: js-worker (runs reclaimed until they give up, or stopped
    heartbeating);
  - `dispatch_error` (`ProtoNotFoundError`, `ProtoInvalidError`): js-worker, then builds;
  - `rejected_unavailable`: js-backend and js-worker (admission refused);
  - `unknown`: js-worker; if its errors name tool calls or the connector, `connector`.
- **Reads:** deploy logs for a recent deploy of that service if any; logs for that
  service; `get_service_instance` for it. Add logs of a second service only if the first
  points at it.
- **Normal:** `platform_class` at or under the threshold; customer-class kinds
  (`story_error`, `credential_unresolved`, `rejected_no_credits`) do not count.
- **Bad:** one platform kind dominates; the same exception repeats in the logs.
- **Likely causes → recommendation:**
  - Failures start right after a deploy → `rollback_recent_deploy` for that service.
  - `stalled` / `dead_letter` with worker restarts or out-of-memory → `restart_service`
    js-worker.
  - `dispatch_error` or a repeated exception in code → `code_change`.
  - Connector errors (timeouts, 5xx) → `investigate` the connector.

## `relay_backlog`

The oldest undelivered analytics event is older than the threshold (default 300 s).
Warning.

- **Services:** js-worker (runs the relay loop).
- **Reads:** logs js-worker; `get_service_instance` js-worker.
- **Normal:** `relay.oldest_undelivered_age_s` under the threshold; `undelivered` small.
- **Bad:** undelivered keeps growing; delivery errors (timeouts, 4xx/5xx from the
  destination) in the logs; or the worker is down (`dispatch_stale` firing too).
- **Likely causes → recommendation:**
  - Worker down or stale → `restart_service` js-worker (or the `dispatch_stale` cause).
  - Destination rejects or times out → `investigate` the destination.
  - No errors and a healthy worker → `investigate`, confidence `unsure`.

## `relay_dead_letters`

At least one analytics delivery was dead-lettered in the last 15 minutes. Warning.

- **Services:** js-worker.
- **Reads:** logs js-worker (ERROR, then WARNING if empty).
- **Normal:** `relay.dead_letters_15m` 0.
- **Bad:** the same delivery error repeats for each dead letter.
- **Likely causes → recommendation:**
  - The destination rejects the payload (4xx, schema error) → `code_change`.
  - Destination auth or outage (401, 403, 5xx) → `investigate` the destination.
  - After a deploy → `rollback_recent_deploy` js-worker.

## `build_backlog`

The oldest queued agent build is older than the threshold (default 600 s). Warning.

- **Services:** js-worker (runs the build loop).
- **Reads:** logs js-worker; `get_service_instance` js-worker; `get_service_metrics`
  js-worker.
- **Normal:** `builds.oldest_queued_age_s` under the threshold; `failed_15m` 0.
- **Bad:** builds queue while `failed_15m` rises (builds fail and retry), or none
  complete (build loop stuck), or the worker is at its CPU or memory limit.
- **Likely causes → recommendation:**
  - Build loop stuck, no errors → `restart_service` js-worker.
  - Builds failing with the same error → `code_change`.
  - After a deploy → `rollback_recent_deploy` js-worker.
  - Worker at its limit → `scale_worker`.

## Any other rule

- **Services:** any service named in the event (`recent_deploy.service`); else
  js-backend and js-worker.
- **Reads:** `get_service_instance` for each (at most 2); logs for the one that looks
  unhealthy.
- **Recommendation:** `investigate`, unless the evidence clearly matches a cause above.
  If js-monitor itself looks wrong (for example it keeps restarting), read `js-monitor`
  logs if its id is configured; never recommend more than one js-monitor replica.
