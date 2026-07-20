from __future__ import annotations

from app.hosts.service_status_push import GUARDED_SECTIONS, HEALTH_SECTIONS


def test_device_health_is_guarded_and_off_the_synchronous_path() -> None:
    # Phase 4: both health axes are stamped and folded by the StatusFoldLoop.
    assert "node_health" in GUARDED_SECTIONS
    assert "device_health" in GUARDED_SECTIONS
    assert set(GUARDED_SECTIONS) == set(HEALTH_SECTIONS)


def test_synchronous_folds_keep_only_the_cheap_sections() -> None:
    import ast
    from pathlib import Path

    composition = Path(__file__).parents[2] / "app" / "composition.py"
    tree = ast.parse(composition.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "HostStatusPushService"
    ]
    assert len(calls) == 1
    keyword = next(entry for entry in calls[0].keywords if entry.arg == "observation_folds")
    assert isinstance(keyword.value, ast.Tuple)
    folds = {
        entry.args[0].value
        for entry in keyword.value.elts
        if isinstance(entry, ast.Call)
        and entry.args
        and isinstance(entry.args[0], ast.Constant)
        and isinstance(entry.args[0].value, str)
    }
    # Properties and host_telemetry stay synchronous; the two health folds
    # must NOT be here (both fold off the request path on the StatusFoldLoop).
    assert "device_health" not in folds
    assert "node_health" not in folds
    assert {"device_properties", "host_telemetry"} <= folds
