from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"

DESIRED_STATE_IMPORT_ALLOWED = {
    "services/appium_reconciler.py",
    "services/appium_reconciler_agent.py",
    "services/device_verification_execution.py",
    "services/intent_reconciler.py",
}
DESIRED_GRID_RUN_ID_IMPORT_ALLOWED = {
    "services/intent_reconciler.py",
}
DIRECT_ASSIGN_ALLOWED = {
    "models/appium_node.py",
    "models/device.py",
    "services/desired_state_writer.py",
    "services/intent_reconciler.py",
}


def _python_files() -> list[Path]:
    return sorted(ROOT.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_desired_state_writers_are_owned_by_reconcilers() -> None:
    offenders: list[str] = []
    for path in _python_files():
        rel = _relative(path)
        text = path.read_text()
        if "import write_desired_state" in text and rel not in DESIRED_STATE_IMPORT_ALLOWED:
            offenders.append(f"{rel}: write_desired_state")
        if "import write_desired_grid_run_id" in text and rel not in DESIRED_GRID_RUN_ID_IMPORT_ALLOWED:
            offenders.append(f"{rel}: write_desired_grid_run_id")
    assert offenders == []


def test_old_grid_run_id_reconciler_is_removed() -> None:
    offenders = [
        _relative(path)
        for path in _python_files()
        if "grid_node_run_id_reconciler" in path.read_text() or path.name == "grid_node_run_id_reconciler.py"
    ]
    assert offenders == []


def test_derived_orchestration_fields_are_reconciler_owned() -> None:
    fields = (
        "desired_grid_run_id",
        "accepting_new_sessions",
        "stop_pending",
        "recovery_allowed",
        "recovery_blocked_reason",
    )
    offenders: list[str] = []
    for path in _python_files():
        rel = _relative(path)
        if rel in DIRECT_ASSIGN_ALLOWED or rel.startswith("alembic/"):
            continue
        text = path.read_text()
        for field in fields:
            if f".{field} =" in text:
                offenders.append(f"{rel}: {field}")
    assert offenders == []
