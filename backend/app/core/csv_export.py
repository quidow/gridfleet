import csv
import io
from typing import TYPE_CHECKING

from fastapi.responses import StreamingResponse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel


def to_csv_response(rows: Sequence[BaseModel], filename: str) -> StreamingResponse:
    """Convert a list of Pydantic models to a CSV StreamingResponse."""
    buf = io.StringIO()
    if rows:
        columns = list(type(rows[0]).model_fields.keys())
        writer = csv.DictWriter(buf, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.model_dump())
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
