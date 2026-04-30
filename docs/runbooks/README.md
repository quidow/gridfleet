# Runbooks

This directory is for incident-response playbooks.

Use `docs/runbooks/` when the system is already misbehaving and the operator needs exact commands, fast checks, and a narrow recovery path instead of a broad product walkthrough.

## Current Runbooks

- `slow-system.md`
  - Readiness, metrics, Grid, database, and log checks when the stack feels slow or queue-backed.
- `agent-not-connecting.md`
  - Manager-to-agent reachability, service-manager checks, and recovery flow for missing heartbeats.
- `stuck-devices.md`
  - Recovery steps for devices stuck in `busy` or `reserved`.
- `webhook-delivery-failures.md`
  - Delivery history, retry, test-event, and disable/fix workflow for failing outbound webhooks.
- `backend-deploy-restart-rollback.md`
  - Manual production restart, deploy, verification, and rollback procedure for the single-stack compose deployment.
