# Documentation

This directory is for product, operator, and integration documentation only.

## Documentation Taxonomy

- `README.md`
  - Product-doc entry point and navigation hub.
- `guides/`
  - Workflow and task-oriented operator docs.
- `runbooks/`
  - Incident-response procedures with exact commands and recovery steps.
- `reference/`
  - Contract, glossary, and API/settings reference material.

New documentation should prefer `docs/guides/` for narrative workflow material, `docs/runbooks/` for incident response, and `docs/reference/` for lookup-style documentation. Keep `docs/` root reserved for shared entry points and a small number of high-level cross-cutting pages. Note that developer-facing implementation material can also live here if it helps operators understand the architecture.

## Current Docs

### Guides

- `guides/host-requirements.md`
  - Linux/macOS host prerequisites, managed runtime tools, and setup verification checklist.
- `guides/host-onboarding.md`
  - Host registration, approval, and discovery-first onboarding.
- `guides/device-intake-and-discovery.md`
  - Host-first discovery, intake candidates, and add-device lanes.
- `guides/verification-and-readiness.md`
  - Verification stages, readiness states, and retry behavior.
- `guides/lifecycle-maintenance-and-recovery.md`
  - Maintenance, reconnect, lifecycle summaries, and recovery behavior.
- `guides/settings-and-operational-controls.md`
  - Settings tabs, operational knobs, driver-page handoff, and webhook controls.
- `guides/runs-and-reservations.md`
  - Reservation lifecycle, operator run monitoring, and cancel versus force-release guidance.
- `guides/dashboard-and-triage.md`
  - Dashboard interpretation, lifecycle triage, and drill-down workflow.
- `guides/hosts-and-host-detail-operations.md`
  - Daily host operations after onboarding, including discovery and driver sync.
- `guides/groups-and-bulk-actions.md`
  - Static/dynamic groups, ad hoc bulk actions, and maintenance/template/tag workflows.
- `guides/deployment.md`
  - Local development, production compose deployment, backup/restore, and verification.
- `guides/ci-integration.md`
  - Reservation and run orchestration guidance for CI pipelines and test suites.
- `guides/frontend-development.md`
  - React/Tailwind architectural standards and UI development workflows.
- `guides/security.md`
  - Threat models, network boundary definitions, and authorization controls.
- `guides/README.md`
  - Guide index and placement rules for future operator docs.

### Runbooks

- `runbooks/slow-system.md`
  - Queue pressure, metrics, Grid, and Postgres checks for system-wide slowness.
- `runbooks/agent-not-connecting.md`
  - Manager-to-agent reachability and agent service recovery workflow.
- `runbooks/stuck-devices.md`
  - Recovery flow for devices stuck in `busy` or `reserved`.
- `runbooks/webhook-delivery-failures.md`
  - Delivery inspection, retry, test-event, and disable/fix flow for webhooks.
- `runbooks/backend-deploy-restart-rollback.md`
  - Manual deployment, restart, and rollback flow for the production compose stack.
- `runbooks/README.md`
  - Runbook index and placement rules for future incident docs.

### Reference

- `reference/README.md`
  - Reference index and placement rules for future contract docs.
- `reference/architecture.md`
  - Detailed system architecture, node lifecycle, and process boundaries.
- `reference/environment.md`
  - Supported backend, agent, and installer environment variables.
- `reference/api.md`
  - Supported `/api` route surface grouped by domain.
- `reference/settings.md`
  - Runtime settings registry, defaults, validation, and env fallbacks.
- `reference/events-and-webhooks.md`
  - SSE, notifications, webhooks, and emitted event names.
- `reference/glossary.md`
  - Core fleet and contract terms.
- `reference/testkit.md`
  - Supported Python testkit package and examples.
- `reference/capabilities.md`
  - How GridFleet derives Appium capabilities from stored device state.
- `reference/release-policy.md`
  - Versioning, compatibility, and release checklist for public tags.
