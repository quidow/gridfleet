from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tests.grid_node.tools.generate_grid_fixtures import generate_fixture_bundle

if TYPE_CHECKING:
    from pathlib import Path


def test_generate_fixture_bundle_writes_raw_and_decoded_files(tmp_path: Path) -> None:
    generate_fixture_bundle(tmp_path)

    raw = tmp_path / "raw" / "01_node_bringup"
    assert (raw / "bus_hub_to_node.jsonl").stat().st_size > 0
    assert (raw / "bus_node_to_hub.jsonl").stat().st_size > 0
    assert (raw / "http.transcript").stat().st_size > 0

    node_added = json.loads((tmp_path / "decoded" / "01_node_bringup" / "node_added.json").read_text())
    assert node_added["type"] == "NODE_ADDED"
    assert node_added["data"]["nodeId"] == "<NODE_ID>"
