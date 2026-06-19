"""The testkit ``Device`` field names must stay a subset of ``DeviceRead``.

``gridfleet_testkit`` is not installed in the backend dev environment, so the
field names are read from the testkit source with ``ast`` rather than imported.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.devices.schemas.device import DeviceRead

_TESTKIT_DEVICE = Path(__file__).resolve().parents[3] / "testkit" / "gridfleet_testkit" / "device.py"


def _testkit_device_field_names() -> set[str]:
    tree = ast.parse(_TESTKIT_DEVICE.read_text())
    class_def = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Device")
    return {
        stmt.target.id
        for stmt in class_def.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }


def test_testkit_device_fields_are_subset_of_device_read() -> None:
    field_names = _testkit_device_field_names()

    assert field_names, f"parsed no Device fields from {_TESTKIT_DEVICE}"
    missing = field_names - set(DeviceRead.model_fields)
    assert missing == set(), f"testkit Device parses fields absent from DeviceRead: {sorted(missing)}"
