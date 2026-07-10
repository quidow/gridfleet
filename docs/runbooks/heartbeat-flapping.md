# Heartbeat Flapping Runbook

## Symptom

Operators see clusters of `host.heartbeat_lost`, `host.status_changed online→offline`, and `host.circuit_breaker.opened` events for hosts that are visibly running.

## Model

Host liveness is push-recency based: the agent pushes one consolidated status report (`POST /agent/hosts/status`) every `AGENT_STATUS_PUSH_INTERVAL_SEC` (default 10 s), which stamps `Host.last_heartbeat`. Online/offline is **computed at read time** from `last_heartbeat` recency (`app/hosts/liveness.py`), so the API and UI show a host offline as soon as `now - last_heartbeat` exceeds `general.host_offline_after_sec` (default 45 s) — roughly 4-5 missed pushes — even before the sweep's next tick writes the ledger edge. The stored `Host.status` column is only the enrollment axis (`pending`) plus an event ledger: `host_sweep`'s `evaluate_host` is the single edge detector for both directions, so the old paired-flap class (the push handler and the sweep writing the same column from different processes) no longer occurs — each transition emits one `host.status_changed`/`host.heartbeat_lost`. "Flapping" now almost always means **pushes arriving intermittently late**, not the backend failing to reach the agent — the reachability probe (`GET /agent/health` on `general.partition_probe_interval_sec`, default 60 s) is a diagnostic only and does not drive liveness.

## Phase A — Gather Evidence

1. Confirm the forward-direction (agent → backend push) metrics and logs:
   - `gridfleet_host_status_pushes_total{host_id=...}` — should increment roughly every `AGENT_STATUS_PUSH_INTERVAL_SEC`; gaps longer than `general.host_offline_after_sec` explain the flap directly.
   - Agent logs for `status push failed` (`agent_app/status_push.py`) — an exception on the agent's own push attempt (network, backend 5xx, timeout).
   - `Host.last_heartbeat` recency in the API/UI vs. `general.host_offline_after_sec` headroom — a host bouncing right at the threshold needs either a longer threshold or a fix to the push cadence, not just a diagnosis.
2. Confirm the reverse-direction (backend → agent reachability probe) metrics are live — useful to rule out a genuine network partition, but they do not explain push-driven flapping on their own:
   - `gridfleet_agent_heartbeat_total{outcome=...,client_mode=...,host_id=...}`
   - `gridfleet_agent_heartbeat_duration_seconds{...}`
   - `background_loop_overrun_total{loop_name="heartbeat"}` (replaces retired `gridfleet_heartbeat_cycle_overrun_total`)
3. If the probe metrics show failures too, run `scripts/diagnose_heartbeat_probe.py` against:
   - the host's currently registered IP (e.g. `192.168.88.249`)
   - the docker bridge gateway (`172.17.0.1` on default bridge — confirm via `ip route`)
   - `host.docker.internal` (must resolve thanks to `extra_hosts: host-gateway` on the backend and router services)
4. Save CSV under `.superpowers/diagnostics/` (gitignored, local-only investigator notes).

## Phase A — Quick Interpretation

| Pattern                                                                                  | Likely root cause                          | Implement |
|-------------------------------------------------------------------------------------------|--------------------------------------------|-----------|
| `gridfleet_host_status_pushes_total` gaps line up with the flap; agent logs `status push failed` | Agent-side push failure (network, backend overload, timeout) | Check agent connectivity/logs first — this is now the dominant cause |
| Push metric is regular but the host still flips; `general.host_offline_after_sec` headroom is tight | Threshold too aggressive for the actual push jitter | Raise `general.host_offline_after_sec` or lower `AGENT_STATUS_PUSH_INTERVAL_SEC` |
| Probe (`gridfleet_agent_heartbeat_total`) also fails, pooled outcomes show timeout while fresh outcomes succeed | Stale pooled connection on the reachability probe | B1 / B3 |
| Probe fails for `192.168.88.249` while `172.17.0.1` and `host.docker.internal` succeed    | Hairpin routing                            | B2        |
| Probe metrics fine but `background_loop_overrun_total{loop_name="heartbeat"}` is high     | Backend overload or pause                  | already mitigated by Tasks 8–10 |
| Both directions fail uniformly                                                            | Agent process / port issue                 | None — file separately |

## Phase A — Stop Criteria

Proceed to Phase B once one of:

- A single incident's CSV + structured logs **localizes a dominant failing boundary** with enough detail to choose a fix, OR
- 3 distinct flap incidents are captured, OR
- 48 hours pass.

If 48 hours pass without a dominant boundary: do **not** implement B1/B2/B3.

## Phase B Decision Tree (reachability-probe path only)

These fixes apply to the backend→agent reachability probe (`GET /agent/health`), not the status push. If evidence points at the push direction instead, fix the agent-side push failure or retune `general.host_offline_after_sec` / `AGENT_STATUS_PUSH_INTERVAL_SEC` — B1/B2/B3 do not apply.

| Evidence                                                          | Implement                                                                                                                            |
|-------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------|
| Pooled requests fail; fresh requests succeed in same window       | B1 — make the probe skip the connection pool: set `agent.http_pool_enabled=false` (drives `_heartbeat_client_mode` / `_pool_enabled` to the fresh branch), or pass a non-default `http_client_factory` / `pool=None` so `_send_request` takes the fresh-client branch. Note the probe already passes `http_client_factory=httpx.AsyncClient`, which is the default factory, so that argument permits pooling rather than forcing a fresh client. |
| Alternate target succeeds while LAN IP fails                      | B2 — set `AGENT_ADVERTISE_IP=host.docker.internal` on the co-located agent and re-register; remote agents stay on UDP-trick discovery |
| Pooled fails intermittently, alternate targets behave identically | B3 — reduce `agent.http_pool_idle_seconds` (60 → 15)                                                                                 |

## Co-located Deployment Guidance

Agents on the same host as the backend container should set `AGENT_ADVERTISE_IP` to a container-reachable address. Acceptable values: any DNS name or IP that the backend container can resolve, including:

- `host.docker.internal` (preferred; `extra_hosts: host-gateway` is wired into the backend and router services in `docker/docker-compose.yml`)
- `172.17.0.1` (default docker0 gateway on Linux)
- The host's stable LAN IP (only if hairpin routing is reliable)

Re-register the agent after changing this value (restart the agent process).
