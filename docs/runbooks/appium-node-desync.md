# Appium node desync runbook

**Symptom:** UI shows `effective_state=stopped` but the host agent still has the
Appium process running.

**Resolution:** wait one reconciler cycle (default 30s, see
`appium_reconciler.interval_sec`). The reconciler reaps orphans and converges
divergence automatically. If it does not:

1. Verify leader election is live: `SELECT last_heartbeat_at, NOW() -
   last_heartbeat_at AS age FROM control_plane_leader_heartbeats WHERE id = 1;`
   (single-row table keyed on `id = 1`). The heartbeat should be only a few
   seconds old — a healthy leader renews it every
   `general.leader_keepalive_interval_sec` (default 5s); a heartbeat older than
   `general.leader_stale_threshold_sec` (default 30s) is considered stale and
   triggers non-leader preemption.
2. Verify the agent is reachable from the backend: call `GET /agent/health` on
   the host's `ip:agent_port` directly.
3. Check `/metrics` for `appium_reconciler_cycle_failures_total`. If it is
   increasing, inspect backend logs for `appium_reconciler_cycle_failed`.
4. Check the `appium_nodes` row's `transition_token` and the device row's
   `lifecycle_policy_state`. A stuck token blocks dispatch; a future
   `backoff_until` value inside the `lifecycle_policy_state` JSON skips
   convergence until that deadline.

**Manual override (last resort):** for a stuck `transition_token`, use
`POST /api/admin/appium-nodes/{node_id}/clear-transition` or the device-detail
"Force-clear restart" button. For a stuck agent process, call
`POST /agent/appium/stop {"port": N}` against the host agent. File an incident
if either override is needed.
