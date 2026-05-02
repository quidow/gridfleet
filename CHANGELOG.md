# GridFleet — Project Highlights

This file summarizes notable project-wide milestones. For detailed per-component changelogs, see:

- [Backend](backend/CHANGELOG.md)
- [Agent](agent/CHANGELOG.md) — `gridfleet-agent` on [PyPI](https://pypi.org/project/gridfleet-agent/)
- [Frontend](frontend/CHANGELOG.md)
- [Testkit](testkit/CHANGELOG.md) — `gridfleet-testkit` on [PyPI](https://pypi.org/project/gridfleet-testkit/)

For version policy and compatibility contracts, see [docs/reference/release-policy.md](docs/reference/release-policy.md).

---

## 0.1.0 — Initial Public Preview

- Initial public preview baseline for the GridFleet control plane.
- Includes the FastAPI backend, host agent, React operator UI, Docker Compose deployment, curated driver-pack manifests, adapter source, and Python testkit package.
- The public API and operational contracts are usable but not yet guaranteed stable across minor releases.
