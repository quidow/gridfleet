from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

from tests.grid_node.tools.record_grid_bus import decode_frames, encode_frames, write_record

if TYPE_CHECKING:
    from pathlib import Path


def test_encode_frames_round_trips_arbitrary_multipart_bytes() -> None:
    frames = [b"topic", b'{"type":"NODE_ADDED"}', b"\x00\xff"]
    encoded = encode_frames(frames)
    assert encoded == [base64.b64encode(frame).decode("ascii") for frame in frames]
    assert decode_frames(encoded) == frames


def test_write_record_writes_jsonl_shape(tmp_path: Path) -> None:
    out = tmp_path / "bus_node_to_hub.jsonl"
    write_record(out, ts=123.5, frames=[b"topic", b'{"type":"NODE_STATUS"}'])
    line = out.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload == {
        "ts": 123.5,
        "frames_b64": [
            base64.b64encode(b"topic").decode("ascii"),
            base64.b64encode(b'{"type":"NODE_STATUS"}').decode("ascii"),
        ],
        "decoded": {"type": "NODE_STATUS"},
    }


def test_write_record_sets_decoded_null_for_non_json_payload(tmp_path: Path) -> None:
    out = tmp_path / "bus_hub_to_node.jsonl"
    write_record(out, ts=1.0, frames=[b"topic", b"\xff"])
    assert json.loads(out.read_text(encoding="utf-8"))["decoded"] is None
