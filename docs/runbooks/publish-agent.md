# Publish GridFleet Agent

This runbook covers publishing only the `gridfleet-agent` Python package. The package currently provides the runnable `gridfleet-agent serve` entry point. Host installation, Selenium JAR setup, and systemd/launchd integration still use the repository shell scripts.

## One-time PyPI setup

Configure Trusted Publishing for both TestPyPI and PyPI:

- Project name: `gridfleet-agent`
- Owner: `quidow`
- Repository: `gridfleet`
- Workflow: `publish-agent.yml`
- Environment: `testpypi` for TestPyPI, `pypi` for PyPI

Use GitHub environments named `testpypi` and `pypi`; require manual approval on the `pypi` environment.

## Publish flow

1. Update `agent/pyproject.toml` version and `agent/agent_app/__init__.py` `__version__` together.
2. Run `cd agent && uv lock`.
3. Run `cd agent && uv run --locked --extra dev pytest -q`.
4. Run `cd agent && uv build --no-sources`.
5. Trigger the `Publish Agent` workflow with `repository=testpypi`.
6. Verify install from TestPyPI in a clean environment.
7. Trigger the `Publish Agent` workflow with `repository=pypi`.

The workflow rebuilds the package, runs formatting, lint, type checking, tests, and smoke-installs both the wheel and source distribution before publishing.
