# Appium node desync runbook

**Symptom:** UI shows `effective_state=stopped` but the host agent still has the
Appium process running.

**Resolution:** wait one host-sweep cycle (default 15s, see
`general.heartbeat_interval_sec`). The reconciler reaps orphans and converges
divergence automatically. If it does not:

1. Verify the scheduler is running its loops. Loops run in one dedicated
   scheduler process (the prod `backend-scheduler` service, or the in-process
   default in dev). Check `/api/health` `checks.background_loops`: every loop
   should report `healthy: true` with a recent `last_succeeded_at`; a stalled
   `host_sweep` means the scheduler wedged. Confirm exactly one process
   holds the singleton loop-runner lock with
   `SELECT pid, granted FROM pg_locks WHERE locktype = 'advisory' AND objid = 6001;`.
   If the scheduler is wedged, restart it (`docker compose restart
   backend-scheduler`) — compose restart re-acquires the advisory lock and
   resumes the loops.
2. Verify the agent is reachable from the backend: call `GET /agent/health` on
   the host's `ip:agent_port` directly.
3. Check `/metrics` for `appium_reconciler_cycle_failures_total`. If it is
   increasing, inspect backend logs for `host_sweep_cycle_failed` and
   `host_sweep_convergence_failed`.
4. Check the `appium_nodes` row's `restart_requested_at` / `started_at` and the
   device row's `lifecycle_policy_state`. An unsatisfied restart watermark
   (`started_at < restart_requested_at`) projects the node as `restarting` and
   blocks dispatch, but only for `appium_reconciler.restart_window_sec`; a future
   `backoff_until` value inside the `lifecycle_policy_state` JSON skips
   convergence until that deadline.

**Manual override (last resort):** a stuck `restarting` projection is not one — it
self-clears at read time once the watermark is satisfied (the agent respawned) or
after `appium_reconciler.restart_window_sec` elapses; there is no clear-transition
route or "Force-clear restart" button anymore. For a stuck agent process, call
`POST /agent/appium/stop {"port": N}` against the host agent. File an incident if
that override is needed.

> **All supported hosts use pull-only orchestration:** a direct
> `POST /agent/appium/stop` is reverted on the agent's next pull while the
> backend still desires the node running. Change desired state through the
> backend instead (e.g. set the device to maintenance / stop the run), then let
> the agent converge.
