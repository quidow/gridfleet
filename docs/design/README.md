# Design Docs — Device & Node Flows

Implementation-level reference for the flows that move a device from "plugged in" to "running a test session". Companion to the operator-facing material under `docs/guides/` and `docs/runbooks/`.

These docs exist because the recurring class of node/device bugs (split-brain between DB and agent, snapshot drift, port leaks, transient-blip flapping) came from code paths that violated invariants the system depends on but had not written down. Each doc is the contract for one set of those invariants.

## The prune doctrine

These docs carry **rationale, invariants, and pointers — never mechanical enumerations the code or a reference doc already owns.** Writer tables live in `PROTECTED_COLUMN_WRITERS` (`backend/tests/contracts/test_no_direct_device_state_writes.py`); the loop roster lives in `_build_leader_loop_tasks` (`backend/app/main.py`); the dial catalog lives in `backend/app/agent_comm/operations.py`; event names live in `docs/reference/events.md`. Exactly two enumerations remain in these docs — doc 3's loop roster and doc 4's dial catalog — and both are pinned in both directions by `backend/tests/contracts/test_design_doc_parity.py`.

If you are about to paste a table of code facts into a design doc: add a pointer to the owner instead, or — if the table genuinely earns its keep — add a parity test in the same PR. Commit hashes and phase-migration history do not belong here; git owns history.

## Reading order

| # | Doc | What it covers |
| --- | --- | --- |
| 1 | [Device State Model](01-device-state-model.md) | The independent axes of device state, the read-time projection, and the locking invariant |
| 2 | [Node Lifecycle](02-node-lifecycle.md) | Pull-model start/stop/restart, the restart watermark, split-brain prevention rules |
| 3 | [Health & Reconciliation Loops](03-health-and-reconciliation.md) | Scheduler launch guard, the pinned loop roster, tri-state probe, one-home-per-fact |
| 4 | [Backend ↔ Agent Contract](04-backend-agent-contract.md) | The pinned dial catalog, push surface, failure model, circuit breaker, versioning |
| 5 | [Allocations, Ports, Sessions](05-allocation-ports-sessions.md) | Typed claims, port authority, backend-owned session create, run integration |

Read in order; each doc assumes the previous ones. Open follow-up specs, when present, live under `docs/design/specs/` — the numbered docs describe what is true today; specs describe proposals.

## When to update these

- A change that breaks a pinned table fails `test_design_doc_parity` — update the table in the same PR.
- A new split-brain class of bug: distil the violated invariant into the relevant doc's checklist (Doc 2 has the canonical example).
- A removed feature: remove the doc rows; these are reference docs, not change logs.

## Citation baseline

References cite **file paths and function/class names**, not line numbers or commit hashes. If a citation no longer points to a real symbol, search by symbol name and update in place. Last citation refresh: 2026-07-13.

## Companion docs

- `docs/reference/device-lifecycle.md` — the canonical `operational_state` writer model and remediation ladder (design docs summarize it, never contradict it)
- `docs/guides/lifecycle-maintenance-and-recovery.md` — operator semantics for maintenance, reconnect, lifecycle states
- `docs/guides/verification-and-readiness.md` — verification stages, readiness gates
- `docs/guides/device-intake-and-discovery.md` — device intake lanes
- `docs/runbooks/` — incident response with exact commands
- `docs/reference/architecture.md` — high-level system shape
- `docs/reference/api.md` — public API surface
