"""Pin /openapi.json output so generated frontend types stay reproducible."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.main import app

if TYPE_CHECKING:
    from pathlib import Path


def test_openapi_schema_is_byte_stable_across_calls() -> None:
    """Two consecutive calls to app.openapi() must produce identical JSON.

    If this fails, something in the FastAPI route registration introduced
    nondeterminism (e.g. a dict ordered by hash, a dynamically registered
    schema). Generated frontend types in src/api/openapi.ts depend on this.
    """
    first = json.dumps(app.openapi(), sort_keys=True)
    # Force re-build by clearing FastAPI's cached schema.
    app.openapi_schema = None
    second = json.dumps(app.openapi(), sort_keys=True)
    assert first == second


def test_dump_openapi_cli_writes_sorted_json(tmp_path: Path) -> None:
    from app.scripts.dump_openapi import main

    out = tmp_path / "openapi.json"
    main(["--out", str(out)])
    raw = out.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    # Round-trip with sort_keys=True must be a no-op on the file content.
    assert raw.rstrip("\n") == json.dumps(parsed, indent=2, sort_keys=True)
    assert parsed["info"]["title"] == "GridFleet"
