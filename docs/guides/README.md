# Guides

This directory is for task-oriented and workflow-oriented documentation.

Use `docs/guides/` when the reader is trying to accomplish something, such as:

- onboarding a host
- discovering or adding a device
- running verification or recovering a device
- operating reservations, dashboard workflows, or host/group actions

Guide pages should explain:

- what the workflow is for
- when to use it
- the main steps in operator language
- important blockers, failure modes, and recovery paths

## Current Guide Set

- `host-onboarding.md`
  - Host self-registration, approval, first checks, and discovery entry points.
- `host-requirements.md`
  - Linux/macOS setup requirements, agent-managed process tools, and host verification checklist.
- `device-intake-and-discovery.md`
  - Host-scoped discovery, intake candidates, manual fields, and lane-specific add-device behavior.
- `verification-and-readiness.md`
  - Readiness states, verification stages, retries, and verified save behavior.
- `lifecycle-maintenance-and-recovery.md`
  - Lifecycle summaries, maintenance, reconnect, and automatic/manual recovery behavior.
- `settings-and-operational-controls.md`
  - Settings categories, control-loop impact, driver-page handoff, and webhook operations.
- `runs-and-reservations.md`
  - Reservation lifecycle, run inspection, and operator actions for stuck reservations.
- `dashboard-and-triage.md`
  - Dashboard card meaning, lifecycle triage, and drill-down workflow.
- `hosts-and-host-detail-operations.md`
  - Daily host operations after onboarding, including discovery, capabilities, and driver sync.
- `groups-and-bulk-actions.md`
  - Static/dynamic groups plus whole-group and ad hoc bulk operations.
- `deployment.md`
  - Stack bring-up plus host install/setup instructions.
- `ci-integration.md`
  - Reservation and CI orchestration guidance.

This guide set now covers the main operator workflows. Reference-style contract and glossary material belongs in `docs/reference/`.
