import csv
import io
from collections.abc import Sequence

from fastapi.responses import StreamingResponse
from pydantic import BaseModel


def to_csv_response(rows: Sequence[BaseModel], filename: str) -> StreamingResponse:
    """Convert a list of Pydantic models to a CSV StreamingResponse."""
    if not rows:
        buf = io.StringIO()
        buf.write("")
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    columns = list(type(rows[0]).model_fields.keys())
    buf = io.StringIO()
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
