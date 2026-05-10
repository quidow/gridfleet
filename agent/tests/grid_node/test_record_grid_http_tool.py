from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

from tests.grid_node.tools.record_grid_http import strip_hop_headers, write_http_record

if TYPE_CHECKING:
    from pathlib import Path


def test_strip_hop_headers_removes_connection_specific_names() -> None:
    headers = {
        "connection": "keep-alive",
        "transfer-encoding": "chunked",
        "content-type": "application/json",
    }
    assert strip_hop_headers(headers) == {"content-type": "application/json"}


def test_write_http_record_uses_jsonl_with_base64_body(tmp_path: Path) -> None:
    transcript = tmp_path / "http.transcript"
    write_http_record(
        transcript,
        ts=5.0,
        kind="request",
        direction="hub_to_node",
        method="POST",
        path="/session",
        headers={"content-type": "application/json"},
        body=b'{"x":1}',
    )
    record = json.loads(transcript.read_text(encoding="utf-8"))
    assert record == {
        "body_b64": base64.b64encode(b'{"x":1}').decode("ascii"),
        "direction": "hub_to_node",
        "headers": {"content-type": "application/json"},
        "kind": "request",
        "method": "POST",
        "path": "/session",
        "ts": 5.0,
    }
