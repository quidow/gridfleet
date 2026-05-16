# Doc 4 — Backend ↔ Agent Contract

> HTTP contract between the FastAPI manager and the FastAPI host agents. Covers endpoint catalog, ack semantics, failure model, circuit breaker, auth surface, and idempotency.

GridFleet has two HTTP-speaking processes per host: the centralised backend and the per-host agent. Most traffic flows backend→agent; the agent talks back only for two flows (desired-pack pull and pack-state status push). All host-aware logic on the backend lives behind the `agent_operations` typed wrapper (`backend/app/services/agent_operations.py`).

This doc specifies that contract.

## Topology

```mermaid
flowchart LR
    classDef be fill:#fef9e7,stroke:#a37c00,color:#000
    classDef ag fill:#e7f0fe,stroke:#1c4ea3,color:#000
    classDef ext fill:#e9f7ec,stroke:#1f7a3a,color:#000

    backend["FastAPI backend<br/>multi-worker, leader-elected"]:::be
    agent1["Host agent A<br/>FastAPI 5100"]:::ag
    agent2["Host agent B<br/>FastAPI 5100"]:::ag
    grid["Selenium Grid hub<br/>4444"]:::ext
    appium1["Appium<br/>4723 to 4823"]:::ext
    appium2["Appium<br/>4723 to 4823"]:::ext

    backend -->|"agent HTTP"| agent1
    backend -->|"agent HTTP"| agent2
    agent1 -->|"desired packs, status, host registration"| backend
    agent2 -->|"desired packs, status, host registration"| backend
    agent1 -.spawns.-> appium1
    agent2 -.spawns.-> appium2
    appium1 --> grid
    appium2 --> grid
```

The CI runner / test client speaks **only** to the Selenium Grid hub for sessions. The backend never proxies WebDriver traffic. Routing of WebDriver requests to the right Appium relay is owned by Grid's capability matcher, not by the backend.

## Auth surface

The two directions are asymmetric today.

- **Backend → agent.** Optional HTTP Basic auth is supported. The backend sends credentials from `GRIDFLEET_AGENT_AUTH_USERNAME` / `GRIDFLEET_AGENT_AUTH_PASSWORD` via `_agent_basic_auth` in `backend/app/agent_client.py`. The agent enforces Basic auth on every `/agent/*` HTTP route when `AGENT_API_AUTH_USERNAME` / `AGENT_API_AUTH_PASSWORD` are set, through `agent/agent_app/api_auth.py:BasicAuthMiddleware`. Leave all four unset for local dev or a trusted private lab network.
- **Agent → backend.** `agent/agent_app/lifespan.py` and `agent/agent_app/registration.py` construct `httpx.BasicAuth(manager_auth_username, manager_auth_password)` from `AGENT_MANAGER_AUTH_USERNAME` / `AGENT_MANAGER_AUTH_PASSWORD` when configured. Used for `/agent/driver-packs/desired`, `/agent/driver-packs/status`, and host registration. This satisfies backend machine auth when `GRIDFLEET_AUTH_ENABLED=true`.
- **Agent terminal WebSocket.** The agent Basic-auth middleware only covers HTTP scopes. `/agent/terminal` is guarded separately by `AGENT_TERMINAL_TOKEN` through `agent/agent_app/terminal/ws.py`.
- **Browser → backend** (out of scope for this doc). Session cookie + CSRF for non-GET; that path never hits agents directly.

There is no HMAC or message signing. When the optional backend→agent Basic-auth credentials are unset, transport security relies entirely on the network boundary documented in `docs/guides/security.md`.

## Endpoint catalog (backend → agent)

All paths are under `http://<host_ip>:<host.agent_port>`. The wrapper module is `backend/app/services/agent_operations.py`.

| Method | Path | Caller (backend) | Purpose | Ack semantics |
| --- | --- | --- | --- | --- |
| GET | `/agent/health` | `heartbeat_loop` | liveness + version + missing prerequisites | 200 → ok; non-200 → `None` (treated as missed heartbeat) |
| GET | `/agent/host/telemetry` | `host_resource_telemetry_loop` | CPU/memory/disk numbers | 200 → snapshot; non-200 → `None` |
| GET | `/agent/pack/devices` | `device_connectivity_loop`, intake/discovery | currently-visible devices per pack | 2xx required (raises on non-2xx) |
| GET | `/agent/pack/devices/{ct}/properties` | `property_refresh_loop` | per-device props (OS version, model, etc.) | 200 → dict, 404 → `None`, other → raise |
| GET | `/agent/pack/devices/{ct}/health` | verification flow | adapter-driven health probe | 200 → dict, otherwise → raise |
| GET | `/agent/pack/devices/{ct}/telemetry` | `hardware_telemetry_loop` | adapter-driven hardware telemetry | 200 → dict, 404 → `None` |
| POST | `/agent/pack/devices/{ct}/lifecycle/{action}` | lifecycle/operator actions | run a pack-defined lifecycle action (e.g. boot, shutdown) | 2xx required |
| POST | `/agent/pack/devices/normalize` | intake/discovery | normalise raw input to canonical device fields | 200 → dict, 404 → `None` |
| POST | `/agent/pack/features/{feat}/actions/{act}` | feature dispatch | dispatch arbitrary pack feature action | 2xx required |
| POST | `/agent/appium/start` | `node_service.start_node`, `restart_node_via_agent` | spawn an Appium node | 2xx → `{pid, port, connection_target}` |
| POST | `/agent/appium/stop` | `node_service.stop_node`, `restart_node_via_agent` | kill an Appium node | wrapper returns `httpx.Response`; `stop_remote_temporary_node` maps 2xx → `True`, transport/HTTP error → `False` |
| GET | `/agent/appium/{port}/status` | `node_health`, `_wait_for_remote_appium_ready` | "is the Appium on this port up?" | 200 → `{running: bool}`; non-200 → `None` |
| GET | `/agent/appium/{port}/logs` | host detail UI | return last N lines | 2xx required |
| GET | `/agent/plugins` | plugin sync flow | currently-installed plugins | 2xx required |
| POST | `/agent/plugins/sync` | plugin sync flow | install/remove plugin set | 2xx required |
| GET | `/agent/tools/status` | host onboarding | Node provider and host helper versions | 2xx required |
| WS | `/agent/terminal` | host terminal feature | interactive shell over WebSocket | out of scope here |

Each row has a typed function in `agent_operations.py`. The function signature pins the response shape and the ack contract (`bool`, `bool | None`, `dict | None`, etc.). Routers and services should never call `httpx` directly — go through these wrappers so the circuit breaker and metrics fire.

### `/agent/appium/start` cap surfaces

The Appium start payload carries three distinct capability surfaces. They have separate sources of truth and separate consumers; keep them disentangled when extending the contract.

| Field             | Source of truth                                                                                                                                                             | Consumer                                                                                |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `stereotype_caps` | Pack manifest `capabilities.stereotype` (with `{device.*}` interpolation) + manager-owned sentinels (`appium:gridfleet:deviceId`, `gridfleet:run_id`) + tag fanout          | Selenium Grid hub: stored per-slot, matched against client requests, exposed on `/status` |
| `extra_caps`      | `build_extra_caps(device, ...)` — full device dump (platform, os_version, manufacturer, model, ip, deviceName, sanitized `device_config.appium_caps`, tags, allocated caps) | Agent relay: merged into Appium `/session` request body (`agent/agent_app/appium/process.py`) |
| `allocated_caps`  | `appium_node_resource_service.get_capabilities(...)` (UDID + reserved ports)                                                                                                | Agent → Appium driver                                                                   |

**Cross-component invariant.** The Grid slot stereotype is the routing surface. Backend MUST NOT include Appium-only device metadata (manufacturer, model, ip, deviceName, sanitized `device_config.appium_caps`) in `stereotype_caps`. That metadata MUST flow via `extra_caps` only. The agent enforces the same separation in `agent_app/grid_node/protocol.build_slots` and `agent_app/appium/process.AppiumProcessManager._start_grid_node_service`. The `gridfleet:available` sentinel was removed in release 2026.05 — `AppiumNode.accepting_new_sessions` plus Selenium `NodeStatus.availability` cover the routing-suppression cases.

## Endpoint catalog (agent → backend)

| Method | Path | Caller (agent) | Purpose | Ack semantics |
| --- | --- | --- | --- | --- |
| POST | `/api/hosts/register` | bootstrap | one-time host registration | 2xx, returns `Host` row id |
| GET | `/agent/driver-packs/desired` | `PackStateLoop` (~10 s) | desired pack list for this host | 200 → `{packs: [...]}` |
| POST | `/agent/driver-packs/status` | `PackStateLoop` after each tick | report runtime/adapter state | 204 |

Defined in `backend/app/routers/agent_driver_packs.py`. Note: there is **no agent-initiated callback for node state changes**. The agent reports node lifecycle only by responding to backend polls such as Appium status — the backend pulls, the agent does not push. This is intentional and important: it means the backend is the only authority deciding "is this node up", which is what makes the leader-only health loop sufficient.

## Request envelope

Every backend→agent call goes through `request()` in `backend/app/agent_client.py`:

```text
1. agent_circuit_breaker.before_request(host)   # may raise CircuitOpenError
2. attach REQUEST_ID_HEADER (correlation id)    # build_agent_headers
3. perform httpx call                           # GET or POST, with Basic auth when GRIDFLEET_AGENT_AUTH_* is set
4. classify result:
     status >= 500                  → record_failure (transport-like)
     transport exception            → record_failure (transport)
     anything else                  → record_success
5. record_agent_call metric (host, endpoint, outcome, duration)
```

The wrapper guarantees:

- `AgentUnreachableError` for transport failures (DNS, TCP, TLS, idle timeout).
- `AgentResponseError` for non-2xx responses when the wrapper calls `_raise_for_status`.
- `CircuitOpenError` for hosts in the open state — body includes `retry_after_seconds`.
- `httpx.HTTPStatusError` only escapes when a caller chose to inspect the response itself (e.g. `appium_start` to detect "already in use" details).

## Failure taxonomy

```mermaid
flowchart TD
    call["backend to agent call"] --> q1{"circuit open?"}
    q1 -- yes --> r1["CircuitOpenError"]
    q1 -- no --> q2{"transport ok?"}
    q2 -- no --> r2["AgentUnreachableError"]
    q2 -- yes --> q3{"HTTP status"}
    q3 -- 2xx --> ok["parse payload, caller inspects ack contract"]
    q3 -- 4xx --> r4["caller-specific conflict, pass-through, or AgentResponseError"]
    q3 -- 5xx --> r5["AgentResponseError"]
```

Loop callers map all three terminal errors to `None` (indeterminate). API mutators map them to user-visible 502/503 via the FastAPI exception handlers in `backend/app/errors.py`.

## Circuit breaker

`AgentCircuitBreaker` (`backend/app/services/agent_circuit_breaker.py`).

- **Per host.** State is keyed by host IP/hostname. One bad host does not block others.
- **Failure threshold.** 5 consecutive failures → `open`. Cooldown is 30 s.
- **States.**
  - `closed` — pass through.
  - `open` — short-circuit with `CircuitOpenError(retry_after_seconds=...)`.
  - `half_open` — first probe is allowed through; concurrent probes get `retry_after_seconds=0`. Result decides next state.
- **Counted as failure.** Transport errors and HTTP `>= 500` from the response. 4xx is not a failure (the agent answered, just refused).
- **Events.** `host.circuit_breaker.opened` and `.closed` are published to the event bus when the state actually transitions, surfacing on the dashboard and webhooks.

This is what insulates the leader from "10 hosts unreachable" cascading into 14 loops × 10 hosts × 3 retries every cycle.

## Idempotency expectations

Per endpoint, a brief contract:

| Endpoint | Idempotent? | Notes |
| --- | --- | --- |
| `/agent/health` | yes | Read-only |
| `/agent/host/telemetry` | yes | Read-only |
| `/agent/pack/devices` (GET) | yes | Snapshot of currently-visible devices |
| `/agent/appium/start` | **no** | Caller must allocate a free port first (`candidate_ports`). Re-issuing with the same port and the agent already running on it → `NodePortConflictError`. Re-issuing with a fresh port → second running node. |
| `/agent/appium/stop` | yes | Stop on a port that has nothing returns 2xx. Safe to retry. |
| `/agent/appium/{port}/status` | yes | Read-only. |
| `/agent/appium/{port}/logs` | yes | Read-only |
| `/agent/plugins/sync` | yes | Replaces full plugin set; converges to the requested state |
| `/agent/driver-packs/desired` | yes | Read-only by host_id |
| `/agent/driver-packs/status` | yes | Replaces previous status; full snapshot |

The non-idempotent endpoint is `/agent/appium/start`. That is exactly where the split-brain rules from Doc 2 apply: a port is allocated, the agent is asked to start once, and the manager waits for the readiness probe before flipping DB state. If the agent times out mid-start the manager calls `/agent/appium/stop` to undo before raising. The pattern is "allocate, attempt, verify, persist — or rollback".

## Ack semantics for the lifecycle path

This is the most important part of the contract. Every state-changing call between manager and agent obeys a specific ack rule:

```mermaid
sequenceDiagram
    participant Mgr as Backend
    participant Br as Circuit breaker
    participant Ag as Agent
    Mgr->>Br: before_request
    alt circuit open
        Br-->>Mgr: retry_after
        Mgr-->>Mgr: raise CircuitOpenError ⇒ ack = None
    else
        Br-->>Mgr: pass
        Mgr->>Ag: HTTP request
        alt HTTP 2xx
            Ag-->>Mgr: payload
            Mgr-->>Mgr: parse payload → ack = True or definitive False (per endpoint)
        else HTTP non-2xx
            Ag-->>Mgr: non-2xx
            Mgr-->>Mgr: caller-specific projection: conflict/refused, AgentResponseError, or ack = None
        else transport error
            Mgr-->>Mgr: AgentUnreachableError ⇒ ack = None
        end
    end
```

The agent endpoint whose result is a tri-state probe (`/agent/appium/{port}/status`) projects HTTP shapes into `bool | None`:

- **`appium_status`** (`agent_operations.py`). 200 → `dict` (and the consumer reads `running: bool`). Non-200 → `None`. 

- **`appium_stop`**. `agent_operations.appium_stop` returns the raw response. The caller (`stop_remote_temporary_node`) bridges into the DB-flip rule: `resp.raise_for_status()` success → `True`; `AgentCallError` or `httpx.HTTPError` → `False`; only `True` allows `mark_node_stopped`.

The agent does not expose a WebDriver session probe endpoint. Probe sessions are created by the backend through Selenium Grid so health checks exercise the same routing path as CI clients.

When you add a new state-changing endpoint, follow this pattern: pick an explicit return type (`bool`, `bool | None`, or a dataclass) and document the projection from HTTP into that type at the wrapper layer. Do not let the lifecycle code do its own HTTP error handling — that is what `agent_operations.py` is for.

## Timeouts

Each wrapper picks a default. Override via the `timeout=` argument when the caller's loop has its own deadline:

| Endpoint | Default timeout | Reason |
| --- | --- | --- |
| `/agent/health` | 5 s | liveness ping |
| `/agent/appium/start` | `appium.startup_timeout_sec + 5` (~35 s), or `AVD_LAUNCH_HTTP_TIMEOUT_SECS = 190` for virtual devices | virtual devices boot is slow |
| `/agent/appium/stop` | 10 s | bounded shutdown |
| `/agent/appium/{port}/status` | 5 s | quick check |
| `/agent/appium/{port}/logs` | 10 s | small payload |
| `/agent/plugins` | 15 s | adapter-fetched |
| `/agent/plugins/sync` | 180 s | npm install |
| `/agent/tools/status` | 15 s | local probe |
| `/agent/pack/devices` | 45 s | adapter discovery |

Timeouts are deliberately tight on health-path endpoints so a slow agent does not pin the leader's loops. They are deliberately loose on installer endpoints because operator-initiated install is allowed to take minutes.

## Request correlation

Every request carries a `REQUEST_ID_HEADER` (`X-Request-ID`) injected by `RequestContextMiddleware` on both backend and agent. Logs on both sides bind the request id, so operator-facing traces line up across backend + agent.

When a backend loop initiates a request with no inbound request id bound in structlog context, `build_agent_headers` does not synthesize one; the agent's `RequestContextMiddleware` generates one for the agent-side request and returns it on the response.

## Connection pooling

Backend → agent calls reuse `httpx.AsyncClient` instances pooled by `(host_ip, agent_port)` via `app.services.agent_http_pool.AgentHttpPool`. A pooled client lives for the lifetime of the backend process; on lifespan shutdown the pool drains via `aclose()`.

The pool is opt-in via two guards: `agent.http_pool_enabled` (default `true`) **and** the caller using the default `httpx.AsyncClient` factory. Tests that inject a fake `http_client_factory` always go through the legacy per-call path. This is by design — the explicit-factory seam is used by unit tests and special-purpose call sites, and pooling must not surprise them.

`httpx.Limits(max_keepalive_connections=N, keepalive_expiry=S)` is set per client. `agent.http_pool_max_keepalive` controls N (default 10); `agent.http_pool_idle_seconds` controls S in seconds (default 60).

Auth is not part of the pool key because Basic auth is applied per request, not bound to the pooled `httpx.AsyncClient`. Credential changes are process-env changes; restart the backend process after changing `GRIDFLEET_AGENT_AUTH_*`.

Operational note: pooled clients do not refresh DNS until they are closed. If a host's IP changes mid-flight (lab reorg), restart the backend process — toggling `agent.http_pool_enabled` off only routes new calls through the legacy path; existing pooled clients stay open and resume serving if the toggle is flipped back on. Process restart is the only drain.

## Versioning

There is no formal API version on either side today. The backend records the agent's `version` from `/agent/health` on the `Host` row and computes `agent_version_status` against `agent.min_version` for operator visibility. The bootstrap installer and `/agent/health` `version_guidance` payload help keep agents within compatible ranges. Adding/changing an endpoint requires a coordinated release of backend + agent (`docs/reference/release-policy.md`).

`agent.min_version` is backend-enforced guidance for hosts that report to the current backend. It protects new backend expectations for old agents, but it cannot protect the opposite direction: an old backend calling an endpoint removed from a newer agent. Backend-called endpoint removals are safe only when the backend stops calling the endpoint before or at the same time the agent removes it. Roll those changes out backend-first, or deploy backend and agent together; do not roll newer agents across the fleet while an older backend still depends on the removed endpoint.

When evolving an endpoint:

- Adding a field to a request payload — agents must tolerate unknown fields (FastAPI/Pydantic does by default unless `model_config = {extra: 'forbid'}`).
- Adding a field to a response — backend wrappers must tolerate missing fields (use `payload.get(...)`).
- Renaming or removing — needs a breaking component release in `release-please` and the coordinated rollout model above. Don't.

## Structured error codes

The Appium lifecycle endpoints return structured failure detail as `{"code": "<ENUM_VALUE>", "message": "<human text>"}`. Other agent endpoints may still use ordinary FastAPI `detail` strings or endpoint-specific payloads. The Appium error enum is mirrored on both sides:

- `agent/agent_app/error_codes.py:AgentErrorCode`
- `backend/app/services/agent_error_codes.py:AgentErrorCode`

`backend/tests/test_agent_error_code_parity.py` enforces drift detection. Backend matches `code` via `agent_operations.parse_agent_error_detail`; substring matching on `detail.message` is forbidden.

| Code | Source | Meaning |
| --- | --- | --- |
| `PORT_OCCUPIED` | `appium.process.PortOccupiedError` | External listener already bound the requested port |
| `ALREADY_RUNNING` | `appium.process.AlreadyRunningError` | Managed Appium already running on this port |
| `STARTUP_TIMEOUT` | `appium.process.StartupTimeoutError` | Appium did not become ready in `appium.startup_timeout_sec` |
| `RUNTIME_MISSING` | `appium.process.RuntimeMissingError` / `RuntimeNotInstalledError` | Required runtime tools are absent |
| `DEVICE_NOT_FOUND` | `appium.process.DeviceNotFoundError` | Connection target not visible to the host adapter |
| `INVALID_PAYLOAD` | `appium.process.InvalidStartPayloadError` | Start request missing required fields |
| `INTERNAL_ERROR` | route catch-all | Agent-side state corruption or unclassified adapter failure |

## Open contract questions / known gaps

- **No agent-initiated state push.** Adding webhooks from agent → backend has been discussed but is intentionally absent: it would create a second authority for "is the node up", and the cost of polling at 30 s is acceptable. If we ever change this, every code path in Docs 2 and 3 needs revisiting.
- **No retry budget at the wrapper level.** Loops do their own retry/backoff (`RESTART_MAX_RETRIES`). The wrapper does not retry — that prevents accidental amplification when the agent is degraded.

## What this doc does NOT cover

- Internal node state machine — see Doc 2.
- Loop cadence and reconciliation pattern — see Doc 3.
- Owner allocations, port pools, Grid sessions — see Doc 5.
- Operator-facing onboarding flows — see `docs/guides/host-onboarding.md`.
