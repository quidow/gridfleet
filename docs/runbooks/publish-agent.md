# Publish GridFleet Agent

This runbook covers publishing only the `gridfleet-agent` Python package. The package currently provides the runnable `gridfleet-agent serve` entry point, a safe `gridfleet-agent install --dry-run` preview, `gridfleet-agent install --no-start` for writing config/service files from a dedicated per-OS user-scope venv (`$XDG_DATA_HOME/gridfleet-agent/venv`, fallback `~/.local/share/gridfleet-agent/venv`, on Linux; `~/Library/Application Support/gridfleet-agent/venv` on macOS), `gridfleet-agent install --start` for service enable/start plus local health and manager-registration polling, read-only `gridfleet-agent status`, `gridfleet-agent update` for drain-aware in-place upgrades (`uv pip install --upgrade` into the dedicated venv; requires uv on the host, discoverable or via `--uv-bin`) plus service restart, and confirmed `gridfleet-agent uninstall --yes`.

## One-time PyPI setup

Configure Trusted Publishing for both TestPyPI and PyPI:

- Project name: `gridfleet-agent`
- Owner: `quidow`
- Repository: `gridfleet`
- Workflow: `publish-agent.yml`
- Environment: `testpypi` for TestPyPI, `pypi` for PyPI

Use GitHub environments named `testpypi` and `pypi`; require manual approval on the `pypi` environment.

## Publish flow

1. Set the version in `agent/pyproject.toml` (mirrored into `agent/uv.lock`). This is normally bumped by release-please, not by hand; the runtime `__version__` is read from installed package metadata via `importlib.metadata`, so there is no separate version string to edit.
2. Run `cd agent && uv lock`.
3. Run `cd agent && uv run --locked --extra dev pytest -q`.
4. Run `cd agent && uv build --no-sources`.
5. Trigger the `Publish Agent` workflow with `repository=testpypi`.
6. Verify install from TestPyPI in a clean environment.
7. Trigger the `Publish Agent` workflow with `repository=pypi`.

The workflow rebuilds the package, runs formatting, lint, type checking, tests, and smoke-installs both the wheel and source distribution before publishing.

## Host bootstrap

After publishing, operators can install or upgrade a host with the thin bootstrap wrapper:

```bash
VERSION=0.3.0 bash scripts/install-agent.sh --manager-url http://manager.example.com:8000
```

Run it as the operator who will own the agent service, not as root; the script refuses to run under `sudo`. The wrapper creates or updates the per-OS user-scope venv (`$XDG_DATA_HOME/gridfleet-agent/venv`, fallback `~/.local/share/gridfleet-agent/venv`, on Linux; `~/Library/Application Support/gridfleet-agent/venv` on macOS), installs `gridfleet-agent` from PyPI, and invokes `gridfleet-agent install --start` unless the caller explicitly passes `--dry-run`, `--no-start`, or `--start`.
