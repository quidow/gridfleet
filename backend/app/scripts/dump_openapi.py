"""Dump the FastAPI OpenAPI schema to a file deterministically.

Used by the frontend type-generation workflow so generation does not need a
running uvicorn or a Postgres test database.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING

from app.main import app

if TYPE_CHECKING:
    from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dump GridFleet OpenAPI schema.")
    parser.add_argument(
        "--out",
        required=True,
        help="Path to write the JSON document. Parent directory must exist.",
    )
    args = parser.parse_args(argv)

    schema = app.openapi()
    out_path = Path(args.out)
    out_path.write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
