# Design Docs — Device & Node Flows

Implementation-level reference for the flows that move a device from "plugged in" to "running a test session". Companion to the operator-facing material under `docs/guides/` and `docs/runbooks/`.

These docs exist because the recent class of node/device bugs (split-brain between DB and agent, snapshot drift, port leaks, transient-blip flapping) all came from code paths that violated invariants the system depends on but had not written down anywhere. Each doc is the contract for one set of those invariants.

## Reading order

| # | Doc | What it covers |
| --- | --- | --- |
| 1 | [Device State Model](01-device-state-model.md) | The independent axes of device state, who writes them, and the locking invariant |
| 2 | [Node Lifecycle](02-node-lifecycle.md) | Start/stop/restart sequences, agent-ack contract, split-brain prevention rules |
| 3 | [Health & Reconciliation Loops](03-health-and-reconciliation.md) | Background loop catalog, leader pattern, tri-state probe, snapshot vs source-of-truth |
| 4 | [Backend ↔ Agent Contract](04-backend-agent-contract.md) | HTTP endpoint catalog, failure model, circuit breaker, idempotency |
| 5 | [Allocations, Ports, Sessions](05-allocation-ports-sessions.md) | Owner bundles, port pools, Grid sessions, run integration |

Read in order. Each doc assumes the previous ones; a forward reference is always to a "What this doc does NOT cover" section in another file.

Open follow-up specs, when present, live under `docs/design/specs/`. They are intentionally separate from the implementation-state docs: the numbered docs describe what is true today; specs describe proposed simplifications or reliability fixes that still need implementation.

## When to update these

- A new background loop, a new endpoint, or a new state field — update the corresponding doc *with the same PR* that adds the code.
- A new split-brain class of bug — distil the invariant that was violated and add it to the relevant doc's checklist (Doc 2 has the canonical example).
- A removed feature — remove the doc rows, do not leave stubs. These are reference docs, not change logs.

## Citation baseline

References in these docs cite **file paths and function/class names**, not line numbers. Lines drift as code moves; functions are renamed less often. If a citation no longer points to a real symbol (e.g. a file was consolidated, a function renamed), search by symbol name in the current tree and update the doc in place. Last citation refresh: 2026-05-07.

## Deferred stop note (`stop_pending`)

The current implementation has a `stop_pending` lifecycle path: `session_sync_loop` runs `_sweep_stale_stop_pending`, `lifecycle_policy.clear_pending_auto_stop_on_recovery` clears the intent on health recovery, and lifecycle failure paths set `Device.lifecycle_policy_state["stop_pending"] = true` when an active client session prevents an immediate stop. Doc 1 documents the JSON fields; Doc 3 documents the session-sync backstop.

## Companion docs

These design docs deliberately do not duplicate operator workflow content. For that, see:

- `docs/guides/lifecycle-maintenance-and-recovery.md` — operator semantics for maintenance, reconnect, lifecycle states
- `docs/guides/verification-and-readiness.md` — verification stages, readiness gates
- `docs/guides/device-intake-and-discovery.md` — device intake lanes
- `docs/runbooks/` — incident response with exact commands
- `docs/reference/architecture.md` — high-level system shape
- `docs/reference/api.md` — public API surface
