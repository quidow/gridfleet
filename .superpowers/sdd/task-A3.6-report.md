# Task A3.6 Report: Manifest-load repeat-safety gate

## Status

Implemented the additive lifecycle-action remediation marker and load-time gate. Manifest validation now rejects marked remediation actions whose generic action id is outside the shared repeat-safe allowlist, for both platform base actions and device-type overrides.

## Files

- Modified `backend/app/packs/manifest.py`
  - imports `REPEAT_SAFE_REMEDIATION_ACTIONS` from `app.devices.services.link_repair`;
  - adds `LifecycleAction.remediation: bool = False` without changing `extra="forbid"` or the action-id `Literal`;
  - adds an after-model validator on `Manifest` covering base and override lifecycle actions.
- Created `backend/tests/packs/test_manifest_remediation_gate.py`
  - uses a minimal synthetic YAML manifest;
  - covers marked safe actions, marked unsafe base and override actions, and the unmarked unsafe operator-action default.
- Created `.superpowers/sdd/task-A3.6-report.md`.

No curated manifests were edited.

## RED

Command:

```bash
cd backend && uv run pytest tests/packs/test_manifest_remediation_gate.py -v
```

Result: exit 1, 5 failed. The two marked-safe cases failed because `remediation` was an extra forbidden field; the unsafe cases did not contain the intended `not repeat-safe` error; the unmarked operator action had no `remediation` attribute. Collection completed successfully before the failures.

## GREEN and focused verification

```bash
cd backend && uv run pytest tests/packs/test_manifest_remediation_gate.py -v
```

Result: 5 passed.

The task brief named `tests/packs/test_manifest.py`, which does not exist in this checkout. The existing focused manifest files are `test_manifest_loader.py` and `test_manifest_validation.py`, so the adapted command was:

```bash
cd backend && uv run pytest tests/packs/test_manifest_loader.py tests/packs/test_manifest_validation.py -q
```

Result: 44 passed.

```bash
cd backend && uv run ruff check app/packs/manifest.py tests/packs/test_manifest_remediation_gate.py
```

Result: all checks passed.

```bash
cd backend && uv run mypy app/packs/manifest.py
```

Result: success, no issues in 1 source file.

```bash
cd backend && uv run pytest tests/contracts/test_driver_agnostic_guard.py -q
```

Result: 4 passed.

## Cycle check

The new test module imports `app.packs.manifest`, which imports the shared allowlist from `app.devices.services.link_repair`. An explicit collection-only run confirmed this path is cycle-free at test collection:

```bash
cd backend && uv run pytest --collect-only tests/packs/test_manifest_remediation_gate.py -q
```

Result: all 5 tests collected in 0.01 seconds.

## Self-review

- The validation error identifies the owning platform and action id, contains `not repeat-safe`, and lists `reconnect` and `release_forwarded_ports` as allowed repeat-safe actions.
- Both allowlisted actions are accepted when `remediation: true`.
- `boot` is rejected when marked for remediation but remains valid without the marker, with `remediation` defaulting to `False`.
- The validator iterates each platform's base lifecycle actions and every entry in `device_type_overrides`; the override regression test marks `shutdown` under the `emulator` override and verifies the same rejection details.
- The implementation consumes the existing generic allowlist and adds no platform- or driver-specific branch.
- No dispatch journal, attempt id, conditional runtime safety flag, or curated-manifest marker was added.
- `git diff --check` passed before report generation.

## Concerns and follow-up

No blocking concerns. Importing the allowlist from `link_repair` increases the manifest module's import dependency surface, but the required collection and static checks pass and the task explicitly requires the shared constant as the single policy source. Curated manifests still require separate follow-up work to opt safe actions into automatic remediation.
