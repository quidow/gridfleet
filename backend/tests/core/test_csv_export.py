from pydantic import BaseModel

from app.core.csv_export import to_csv_response


class _Row(BaseModel):
    name: str
    count: int


async def _body(resp: object) -> str:
    chunks = [
        chunk if isinstance(chunk, bytes) else chunk.encode()
        async for chunk in resp.body_iterator  # type: ignore[attr-defined]
    ]
    return b"".join(chunks).decode()


async def test_empty_rows_yield_empty_body_with_csv_headers() -> None:
    resp = to_csv_response([], "empty.csv")
    assert resp.media_type == "text/csv"
    assert resp.headers["content-disposition"] == 'attachment; filename="empty.csv"'
    assert await _body(resp) == ""


async def test_non_empty_rows_emit_header_then_rows() -> None:
    resp = to_csv_response([_Row(name="a", count=1), _Row(name="b", count=2)], "rows.csv")
    assert resp.media_type == "text/csv"
    assert resp.headers["content-disposition"] == 'attachment; filename="rows.csv"'
    lines = (await _body(resp)).splitlines()
    assert lines[0] == "name,count"
    assert lines[1] == "a,1"
    assert lines[2] == "b,2"
