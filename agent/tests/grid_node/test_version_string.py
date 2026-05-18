from agent_app.grid_node.service import _GRID_NODE_VERSION


def test_grid_node_version_matches_hub_pin() -> None:
    assert _GRID_NODE_VERSION == "4.43.0"
