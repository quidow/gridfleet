# Appium node desync runbook

**Symptom:** UI shows `effective_state=stopped` (or deprecated `state=stopped`)
but the host agent still has the Appium process running.

**Resolution:** wait one reconciler cycle (default 30s, see
`appium_reconciler.interval_sec`). The reconciler reaps orphans and converges
divergence automatically. If it does not:

1. Verify leader election is live: `SELECT * FROM control_plane_leader_heartbeats
   ORDER BY heartbeat_at DESC LIMIT 1;` The latest entry should be under 60s old.
2. Verify the agent is reachable from the backend: call `GET /agent/health` on
   the host's `ip:agent_port` directly.
3. Check `/metrics` for `appium_reconciler_cycle_failures_total`. If it is
   increasing, inspect backend logs for `appium_reconciler_cycle_failed`.
4. Check the device row's `transition_token` and `lifecycle_policy_state`. A
   stuck token blocks dispatch; a non-null `backoff_until` skips convergence
   until the deadline.

**Manual override (last resort):** for a stuck `transition_token`, use
`POST /api/admin/appium-nodes/{node_id}/clear-transition` or the device-detail
"Force-clear restart" button. For a stuck agent process, call
`POST /agent/appium/stop {"port": N}` against the host agent. File an incident
if either override is needed.
