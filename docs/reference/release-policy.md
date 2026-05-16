# Release Policy

GridFleet uses independent component releases. Each component — backend, agent, frontend, and testkit — has its own version, changelog, and release cadence. Releases are automated via [release-please](https://github.com/googleapis/release-please) using Conventional Commits.

## Version Scheme

Each component uses SemVer tags with a component prefix:

```text
backend-v0.2.0
agent-v0.1.1
frontend-v0.3.0
testkit-v0.2.0
```

Until a component reaches `v1.0.0`, it is in public preview:

- Patch releases are reserved for backwards-compatible bug fixes, security fixes, documentation corrections, and low-risk dependency updates.
- Minor releases may include breaking changes to that component's public surfaces.
- Major releases are reserved for post-`v1.0.0` compatibility breaks.

## Commit Convention

All commits to `main` must follow [Conventional Commits](https://www.conventionalcommits.org/) with a component scope:

```text
feat(agent): add pack drain endpoint
fix(testkit): handle missing device_id
feat(backend)!: remove v1 sessions API
docs(main): update release policy
```

Allowed scopes: `backend`, `agent`, `frontend`, `testkit`, `docker`, `ci`, `docs`, `deps`, `deps-dev`, `main`.

Breaking changes use `!` after the scope: `feat(backend)!: description`.

release-please reads commits for the release-managed component scopes (`backend`, `agent`, `frontend`, `testkit`) to decide which components need a version bump and what type (patch, minor, major).

Use one of these types when a component-scoped commit should create a release note and version bump:

| Type | Release effect |
| --- | --- |
| `fix(scope): ...` | Patch |
| `perf(scope): ...` | Patch |
| `deps(scope): ...` | Patch |
| `feat(scope): ...` | Minor |
| `type(scope)!: ...` | Major after `v1.0.0`; minor while pre-`v1.0.0` |

Use a release-please type when the change should appear in the component CHANGELOG. For non-release work that still touches a component (refactors, tests, internal docs), keep the component scope with the matching conventional type — e.g. `refactor(backend): ...`, `test(agent): ...`, `chore(frontend): ...`. release-please ignores these for version bumps. Use `(main)`, `(docs)`, `(ci)` scopes only for cross-cutting work.

## Automated Release Flow

1. Developers merge PRs with Conventional Commit messages.
2. release-please opens a Release PR per component with pending changes.
3. The Release PR updates the component's `CHANGELOG.md`, version in `pyproject.toml` or `package.json`, and the `.release-please-manifest.json`.
4. Merging the Release PR creates a GitHub release and a component-prefixed git tag.
5. Tag-triggered CI publishes PyPI packages for `agent` and `testkit`.

## Changelogs

Each component maintains its own changelog:

- `backend/CHANGELOG.md`
- `agent/CHANGELOG.md`
- `frontend/CHANGELOG.md`
- `testkit/CHANGELOG.md`

The root `CHANGELOG.md` is a hand-curated project highlights file updated at significant milestones.

## Compatibility Contract

The following surfaces should be called out explicitly in component release notes when they change:

- backend API routes, request/response schemas, and auth behavior
- database migrations and rollback constraints
- backend-to-agent protocol fields
- agent installer/update behavior
- runtime setting keys and environment variables
- Docker Compose deployment defaults
- testkit package APIs and fixtures
- driver-pack manifest schema, adapter hook contracts, and curated pack IDs
- frontend workflows that change operator behavior

When a backend change requires a minimum agent version (`GRIDFLEET_MIN_AGENT_VERSION`), document the required upgrade path in both the backend and agent changelogs.

## Cross-Component Breaking Changes

When a change in one component breaks compatibility with another (e.g., backend protocol change that requires an agent update):

1. Document the breaking change in the originating component's changelog.
2. Document the required upgrade in the affected component's changelog.
3. Use `!` in the commit scope for the originating component.

## Manual Publish (Fallback)

The `publish-agent.yml` and `publish-testkit.yml` workflows retain `workflow_dispatch` triggers for manual publishes to TestPyPI or PyPI when needed outside the automated flow.

## Driver-Pack Releases

Driver-pack manifest `release` values are independent from component tags. They describe pack content compatibility and ordering inside the manager catalog. When a repository release changes curated driver-pack manifests, document the pack IDs and manifest releases that changed in the backend changelog.
