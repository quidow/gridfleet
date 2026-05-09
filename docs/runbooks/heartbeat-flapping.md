# Heartbeat Flapping Runbook

## Symptom

Operators see clusters of `host.heartbeat_lost`, `host.status_changed online→offline`, and `host.circuit_breaker.opened` events for hosts that are visibly running.

## Phase A — Gather Evidence

1. Confirm new metrics are live:
   - `gridfleet_agent_heartbeat_total{outcome=...,client_mode=...,host_id=...}`
   - `gridfleet_agent_heartbeat_duration_seconds{...}`
   - `gridfleet_heartbeat_cycle_overrun_total`
2. From the backend container, run `scripts/diagnose_heartbeat_probe.py` against:
   - the host's currently registered IP (e.g. `192.168.88.249`)
   - the docker bridge gateway (`172.17.0.1` on default bridge — confirm via `ip route`)
   - `host.docker.internal` (must resolve thanks to `extra_hosts: host-gateway` on the backend service)
3. Save CSV under `.superpowers/diagnostics/` (gitignored, local-only investigator notes).

## Phase A — Quick CSV Interpretation

| Pattern in CSV                                                              | Likely root cause                          | Implement |
|-----------------------------------------------------------------------------|--------------------------------------------|-----------|
| Pooled outcomes show timeout while fresh outcomes succeed                    | Stale pooled connection                    | B1 / B3   |
| `192.168.88.249` fails while `172.17.0.1` and `host.docker.internal` succeed | Hairpin routing                            | B2        |
| All targets succeed but cycle-overrun metric is high                         | Backend overload or pause                  | already mitigated by Tasks 8–10 |
| All targets fail uniformly                                                   | Agent process / port issue                 | None — file separately |

## Phase A — Stop Criteria

Proceed to Phase B once one of:

- A single incident's CSV + structured logs **localizes a dominant failing boundary** with enough detail to choose a fix, OR
- 3 distinct flap incidents are captured, OR
- 48 hours pass.

If 48 hours pass without a dominant boundary: do **not** implement B1/B2/B3.

## Phase B Decision Tree

| Evidence                                                          | Implement                                                                                                                            |
|-------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------|
| Pooled requests fail; fresh requests succeed in same window       | B1 — heartbeat uses fresh client (`http_client_factory=httpx.AsyncClient`)                                                           |
| Alternate target succeeds while LAN IP fails                      | B2 — set `AGENT_ADVERTISE_IP=host.docker.internal` on the co-located agent and re-register; remote agents stay on UDP-trick discovery |
| Pooled fails intermittently, alternate targets behave identically | B3 — reduce `agent.http_pool_idle_seconds` (60 → 15)                                                                                 |

## Co-located Deployment Guidance

Agents on the same host as the backend container should set `AGENT_ADVERTISE_IP` to a container-reachable address. Acceptable values: any DNS name or IP that the backend container can resolve, including:

- `host.docker.internal` (preferred; `extra_hosts: host-gateway` is wired into `docker/docker-compose.yml`)
- `172.17.0.1` (default docker0 gateway on Linux)
- The host's stable LAN IP (only if hairpin routing is reliable)

Re-register the agent after changing this value (restart the agent process).
