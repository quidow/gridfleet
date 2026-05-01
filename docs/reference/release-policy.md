# Release Policy

GridFleet uses coordinated repository releases. A release tag covers the backend, agent, frontend, testkit, Docker Compose files, curated driver-pack manifests, and documentation in this repository.

## Version Scheme

Repository releases use SemVer-shaped tags:

```text
vMAJOR.MINOR.PATCH
```

Until `v1.0.0`, the project is in public preview:

- Patch releases, such as `v0.1.1`, are reserved for backwards-compatible bug fixes, security fixes, documentation corrections, and low-risk dependency updates.
- Minor releases, such as `v0.2.0`, may include breaking changes to API routes, database migrations, deployment settings, agent/backend compatibility, driver-pack behavior, and testkit interfaces.
- Major releases are reserved for post-`v1.0.0` compatibility breaks.

## Compatibility Contract

The following surfaces should be called out explicitly in every release note when they change:

- backend API routes, request/response schemas, and auth behavior
- database migrations and rollback constraints
- backend-to-agent protocol fields
- agent installer/update behavior
- runtime setting keys and environment variables
- Docker Compose deployment defaults
- testkit package APIs and fixtures
- driver-pack manifest schema, adapter hook contracts, and curated pack IDs
- frontend workflows that change operator behavior

Backend and agent versions are released together. If `GRIDFLEET_MIN_AGENT_VERSION` changes, the changelog must explain the required agent upgrade path.

## Release Checklist

Before tagging a public release:

1. Update `CHANGELOG.md`.
2. Ensure package metadata versions match the intended repository version where applicable.
3. Run the normal CI suite.
4. Verify production compose defaults and documented environment variables.
5. Create an annotated Git tag named `vMAJOR.MINOR.PATCH`.
6. Publish release notes from the matching changelog entry.

## Driver-Pack Releases

Driver-pack manifest `release` values are independent from repository tags. They describe pack content compatibility and ordering inside the manager catalog. When a repository release changes curated driver-pack manifests, the repository changelog should summarize the pack IDs and manifest releases that changed.
