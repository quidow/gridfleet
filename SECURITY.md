# Security Policy

GridFleet controls Appium nodes, Selenium Grid routing, host agents, driver-pack execution, and optional host web terminals. Treat every deployment as infrastructure with privileged access to device hosts.

## Supported Versions

Security fixes are accepted against the `main` branch until formal release channels are defined. If tagged releases are introduced later, this policy should be updated with a supported-version table.

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

GridFleet is designed for trusted lab and CI networks. Do not expose the backend, Selenium Grid, agent ports, or host web terminal directly to the public internet.

Use the production auth gate, TLS, and network controls for shared or production-style deployments:

- set `GRIDFLEET_AUTH_ENABLED=true`
- configure operator and machine credentials
- keep `GRIDFLEET_AUTH_COOKIE_SECURE=true` behind HTTPS
- restrict direct access to backend, Grid, and agent ports with VPN, firewall, VPC, or equivalent controls
- rotate secrets after suspected exposure

## High-Risk Features

The following areas deserve special care in reports, reviews, and deployments:

- Host agents can start Appium and Selenium Grid relay processes on device hosts.
- Uploaded driver-pack adapter wheels execute Python code on agent hosts without a sandbox.
- The host web terminal is remote shell access and must stay disabled unless explicitly needed.
- Selenium Grid is not an authentication boundary.
- Device configuration can contain sensitive setup fields such as developer passwords.
- Logs, event streams, screenshots, and database backups may contain device identifiers or lab topology details.

See [docs/guides/security.md](docs/guides/security.md) for deployment and network-boundary guidance.
