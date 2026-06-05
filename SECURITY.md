# Security Policy

GridFleet controls Appium nodes, the WebDriver router, host agents, and driver-pack execution. Treat every deployment as infrastructure with privileged access to device hosts.

## Supported Versions

GridFleet releases each component independently via release-please (per-component tags such as `gridfleet-backend-v*`, `gridfleet-agent-v*`, `gridfleet-frontend-v*`, and `gridfleet-testkit-v*`; the agent and testkit are published to PyPI). Security fixes target the `main` branch and are delivered in the next release of each affected component. Only the latest release of each component, plus `main`, is supported. See [docs/reference/release-policy.md](docs/reference/release-policy.md).

## Reporting A Vulnerability

Please do not open a public issue for a vulnerability.

Use GitHub private vulnerability reporting for this repository. If that is not available, contact the maintainer through the GitHub profile without including exploit details, credentials, hostnames, device identifiers, logs, or other sensitive material in the first message.

Helpful private reports include:

- affected commit, branch, or release
- affected component: backend, agent, frontend, Docker deployment, testkit, or driver pack
- deployment mode: local compose, production compose, or native agent
- whether `GRIDFLEET_AUTH_ENABLED` is enabled
- reproduction steps or proof of concept
- expected impact and any known mitigations

The maintainer will acknowledge valid reports as soon as practical, coordinate a fix privately, and publish public details after a patch or mitigation is available.

## Security Boundaries

GridFleet is designed for trusted lab and CI networks. Do not expose the backend, the WebDriver router, or agent ports directly to the public internet.

Use the production auth gate, TLS, and network controls for shared or production-style deployments:

- set `GRIDFLEET_AUTH_ENABLED=true`
- configure operator and machine credentials
- keep `GRIDFLEET_AUTH_COOKIE_SECURE=true` behind HTTPS
- restrict direct access to backend, router, and agent ports with VPN, firewall, VPC, or equivalent controls
- rotate secrets after suspected exposure

## High-Risk Features

The following areas deserve special care in reports, reviews, and deployments:

- Host agents can start Appium processes on device hosts.
- Uploaded driver-pack adapter wheels execute Python code on agent hosts without a sandbox.
- The WebDriver router is not an authentication boundary.
- Device configuration can contain sensitive setup fields such as developer passwords.
- Logs, event streams, screenshots, and database backups may contain device identifiers or lab topology details.

See [docs/guides/security.md](docs/guides/security.md) for deployment and network-boundary guidance.
