# Runbooks

This directory is for incident-response playbooks.

Use `docs/runbooks/` when the system is already misbehaving and the operator needs exact commands, fast checks, and a narrow recovery path instead of a broad product walkthrough.

## Current Runbooks

- `slow-system.md`
  - Readiness, metrics, Grid, database, and log checks when the stack feels slow or queue-backed.
- `agent-not-connecting.md`
  - Manager-to-agent reachability, service-manager checks, and recovery flow (host approval + discovery refresh) for hosts stuck `pending` or flipped `offline`.
- `stuck-devices.md`
  - Recovery steps for devices stuck in `busy` or `reserved`.
- `backend-deploy-restart-rollback.md`
  - Manual production restart, deploy, verification, and rollback procedure for the single-stack compose deployment.
- `appium-node-desync.md`
  - Recovery when a node's derived `effective_state` (e.g. `stopped`) diverges from the host agent's actual running Appium process — orphaned/stuck process or stuck `transition_token`, converged by the scheduler's `host_sweep` loop.
- `device-export-import.md`
  - Bulk device export and re-import workflow.
- `device-ip-ping-recovery.md`
  - IP connectivity recovery for unreachable devices.
- `grid-version.md`
  - Rebuilding and rolling out the WebDriver router service.
- `heartbeat-flapping.md`
  - Diagnosis and resolution of flapping agent heartbeats.
- `lifecycle-stuck-deferred-stop.md`
  - Recovery for devices stuck in deferred-stop lifecycle state.
- `publish-agent.md`
  - Steps to publish a new agent release to PyPI.
- `publish-testkit.md`
  - Steps to publish a new testkit release to PyPI.
