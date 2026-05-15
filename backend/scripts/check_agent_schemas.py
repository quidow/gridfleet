"""Fail if backend/app/agent_comm/generated.py drifts from the agent OpenAPI.

Usage:
    cd backend && uv run python scripts/check_agent_schemas.py

Run as a CI step and pre-commit hook. The script is sibling to
generate_agent_schemas.py and reuses the same flags / header.
"""

from __future__ import annotations

import difflib
import json
import sys
import tempfile
from pathlib import Path

# Reuse generator constants by importing the sibling.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_agent_schemas import (
    GENERATED_PATH,
    HEADER,
    _fetch_openapi,
    _run_codegen,
    _strip_codegen_header,
)


def main() -> int:
    openapi_payload = _fetch_openapi()
    tmp_dir = Path(tempfile.mkdtemp())
    in_path = tmp_dir / "openapi.json"
    out_path = tmp_dir / "generated.py"
    try:
        in_path.write_text(json.dumps(openapi_payload, sort_keys=True))
        _run_codegen(in_path, out_path)
        fresh = HEADER + _strip_codegen_header(out_path.read_text())
        current = GENERATED_PATH.read_text()
        if fresh != current:
            diff = difflib.unified_diff(
                current.splitlines(keepends=True),
                fresh.splitlines(keepends=True),
                fromfile=str(GENERATED_PATH),
                tofile="<freshly-generated>",
            )
            sys.stdout.writelines(diff)
            print(
                "\nDRIFT: backend/app/agent_comm/generated.py is out of sync with agent OpenAPI."
                "\nRun: cd backend && uv run python scripts/generate_agent_schemas.py",
                file=sys.stderr,
            )
            return 1
    finally:
        in_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        tmp_dir.rmdir()
    print("agent_comm/generated.py is up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
