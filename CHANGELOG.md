# Changelog

All notable public changes to GridFleet are documented here.

This project follows a pre-1.0 compatibility policy. Patch releases are intended to be backwards-compatible fixes, while minor releases may include breaking changes until the public API, deployment contract, and testkit interfaces stabilize. See [docs/reference/release-policy.md](docs/reference/release-policy.md).

## Unreleased

- Prepared the repository for an initial public source release.
- Added public project docs for contributing, security reporting, issue templates, pull requests, and code of conduct.
- Hardened production compose defaults around authentication and host approval.
- Hardened driver-pack tarball upload handling with archive path, member, file type, and size validation.
- Added CI, security scanning, and dependency update workflows.
- Pinned Node.js tooling to Node 24 and tightened Docker image/build-context hygiene.

## 0.1.0 - Initial Public Preview

- Initial public preview baseline for the GridFleet control plane.
- Includes the FastAPI backend, host agent, React operator UI, Docker Compose deployment, curated driver-pack manifests, adapter source, and Python testkit package.
- The public API and operational contracts are usable but not yet guaranteed stable across minor releases.
