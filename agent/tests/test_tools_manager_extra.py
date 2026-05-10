import os
from unittest.mock import patch

from agent_app.tools_manager import (
    NodeProvider,
    _find_fnm_binary,
    _fnm_base_dirs,
    _fnm_default_bin_dirs,
)


def test_node_provider_command_and_fnm_discovery_helpers() -> None:
    provider = NodeProvider(name="system", node_path="/usr/bin/node", npm_path="/usr/bin/npm")
    assert provider.command("node", "--version") == ["/usr/bin/node", "--version"]
    assert provider.command("npm", "install") == ["/usr/bin/npm", "install"]
    assert provider.command("appium", "--version") == ["appium", "--version"]

    with (
        patch("agent_app.tools_manager.shutil.which", return_value=None),
        patch("agent_app.tools_manager._is_executable", side_effect=lambda path: path == "/usr/local/bin/fnm"),
    ):
        assert _find_fnm_binary() == "/usr/local/bin/fnm"

    with patch.dict(os.environ, {"FNM_DIR": "~/custom-fnm", "XDG_DATA_HOME": "~/xdg"}, clear=True):
        bases = _fnm_base_dirs()
    assert os.path.expanduser("~/custom-fnm") in bases
    assert os.path.expanduser("~/xdg/fnm") in bases

    with patch("agent_app.tools_manager.os.path.isdir", side_effect=lambda path: path.endswith("/aliases/default/bin")):
        bins = _fnm_default_bin_dirs()
    assert all(path.endswith("/aliases/default/bin") for path in bins)
