# Publish GridFleet Testkit

This runbook covers publishing only the `gridfleet-testkit` Python package. It does not publish backend, agent, frontend, driver-pack, or Compose artifacts.

## One-time PyPI setup

Configure Trusted Publishing for both TestPyPI and PyPI:

- Project name: `gridfleet-testkit`
- Owner: `quidow`
- Repository: `gridfleet`
- Workflow: `publish-testkit.yml`
- Environment: `testpypi` for TestPyPI, `pypi` for PyPI

Use GitHub environments named `testpypi` and `pypi`; require manual approval on the `pypi` environment.

## Publish flow

1. Update `testkit/pyproject.toml` version.
2. Run `cd testkit && uv lock`.
3. Run `cd testkit && uv run --locked --extra dev --extra appium pytest -q`.
4. Run `cd testkit && uv build --no-sources`.
5. Trigger the `Publish Testkit` workflow with `repository=testpypi`.
6. Verify install from TestPyPI in a clean environment.
7. Trigger the `Publish Testkit` workflow with `repository=pypi`.

The workflow rebuilds the package, runs formatting, lint, type checking, tests, and smoke-installs both the wheel and source distribution before publishing.
