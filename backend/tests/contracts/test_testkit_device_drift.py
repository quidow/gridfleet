"""The testkit ``Device`` field names must stay a subset of ``DeviceRead``.

``gridfleet_testkit`` is not installed in the backend dev environment, so the
field names are read from the testkit source with ``ast`` rather than imported.

Scope: this guards field *names* only — that every field the testkit ``Device``
parses still exists on ``DeviceRead``. It does NOT verify the wire keys
``serialize_device`` emits (the ``GET /devices`` list endpoint has no
``response_model``), field types/nullability, or that ``from_payload`` reads each
field. A ``serialize_device`` key rename, a type change (``DeviceRead.id`` is a
``uuid.UUID``; testkit reads ``str``), or a required→optional flip is out of
scope and will not trip this test.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.devices.schemas.device import DeviceRead

_TESTKIT_DEVICE = Path(__file__).resolve().parents[3] / "testkit" / "gridfleet_testkit" / "device.py"


def _testkit_device_field_names() -> set[str]:
    assert _TESTKIT_DEVICE.exists(), f"testkit Device source not found at {_TESTKIT_DEVICE}"
    tree = ast.parse(_TESTKIT_DEVICE.read_text())
    class_def = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Device"),
        None,
    )
    assert class_def is not None, f"no `Device` class found in {_TESTKIT_DEVICE}"
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
