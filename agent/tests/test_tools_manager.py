from unittest.mock import AsyncMock, patch

from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.manifest import DesiredPack, ToolDependency
from agent_app.pack.runtime_types import AppiumInstallable
from agent_app.tools.manager import (
    CommandResult,
    NodeProvider,
    _detect_fnm_provider,
    _detect_nvm_provider,
    _detect_system_provider,
    _get_node_version,
    detect_node_provider,
    get_tool_status,
)


def _stub_desired_pack(
    pack_id: str,
    tool_deps: list[ToolDependency],
) -> DesiredPack:
    return DesiredPack(
        id=pack_id,
        release="1.0",
        appium_server=AppiumInstallable(
            source="npm",
            package="appium",
            version="==3.0.0",
            recommended=None,
            known_bad=[],
        ),
        appium_driver=AppiumInstallable(
            source="npm",
            package="appium-fake",
            version="==1.0.0",
            recommended=None,
            known_bad=[],
        ),
        platforms=[],
        tool_dependencies=tool_deps,
    )


async def test_get_tool_status_returns_nulls_for_absent_tools() -> None:
    with (
        patch("agent_app.tools.manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._get_node_version", new_callable=AsyncMock, return_value=None),
    ):
        status = await get_tool_status()

    assert status["host"]["node"]["version"] is None
    assert status["host"]["node_provider"]["version"] is None
    assert status["packs"] == {}


async def test_get_tool_status_includes_adapter_tool_versions() -> None:
    class FakeAdapter:
        pack_id = "test-pack"
        pack_release = "1.0"

        def tool_versions(self) -> dict[str, str | None]:
            return {"go_ios": "1.0.207"}

    registry = AdapterRegistry()
    registry.set("test-pack", "1.0", FakeAdapter())  # type: ignore[arg-type]

    desired = [
        _stub_desired_pack(
            "test-pack",
            [
                ToolDependency(name="go_ios", description="iOS telemetry"),
            ],
        ),
    ]

    with (
        patch("agent_app.tools.manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._get_node_version", new_callable=AsyncMock, return_value=None),
    ):
        status = await get_tool_status(adapter_registry=registry, desired_packs=desired)

    pack_tools = status["packs"]["test-pack"]
    assert pack_tools[0]["version"] == "1.0.207"


async def test_get_tool_status_with_provider_error() -> None:
    provider = NodeProvider(
        name="fnm",
        node_path=None,
        npm_path=None,
        error="node_not_configured",
        bin_paths=["/fnm/bin"],
    )
    with (
        patch("agent_app.tools.manager.detect_node_provider", new_callable=AsyncMock, return_value=provider),
        patch("agent_app.tools.manager._get_node_version", new_callable=AsyncMock, return_value=None),
    ):
        status = await get_tool_status()

    assert status["host"]["node_provider"]["version"] is None
    assert status["host"]["node_provider"]["description"] == "node_not_configured"


async def test_get_tool_status_structured_response() -> None:
    class FakeAdapter:
        pack_id = "test-pack"
        pack_release = "1.0"

        def tool_versions(self) -> dict[str, str | None]:
            return {"go_ios": "1.0.207", "xcodebuild": "16.2"}

    registry = AdapterRegistry()
    registry.set("test-pack", "1.0", FakeAdapter())  # type: ignore[arg-type]

    desired = [
        _stub_desired_pack(
            "test-pack",
            [
                ToolDependency(name="go_ios", description="iOS telemetry"),
                ToolDependency(name="xcodebuild", description="Xcode build tools"),
            ],
        ),
    ]

    with (
        patch("agent_app.tools.manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._get_node_version", new_callable=AsyncMock, return_value="20.0.0"),
    ):
        status = await get_tool_status(adapter_registry=registry, desired_packs=desired)

    assert status["host"]["node"]["version"] == "20.0.0"
    assert status["host"]["node"]["name"] == "node"
    assert status["host"]["node_provider"]["version"] is None

    assert len(status["packs"]["test-pack"]) == 2
    go_ios_entry = next(e for e in status["packs"]["test-pack"] if e["name"] == "go_ios")
    assert go_ios_entry["version"] == "1.0.207"
    assert go_ios_entry["description"] == "iOS telemetry"


async def test_get_tool_status_missing_tool_version_is_null() -> None:
    class FakeAdapter:
        pack_id = "test-pack"
        pack_release = "1.0"

        def tool_versions(self) -> dict[str, str | None]:
            return {"go_ios": None}

    registry = AdapterRegistry()
    registry.set("test-pack", "1.0", FakeAdapter())  # type: ignore[arg-type]

    desired = [
        _stub_desired_pack(
            "test-pack",
            [
                ToolDependency(name="go_ios", description="iOS telemetry"),
                ToolDependency(name="xcodebuild", description="Xcode build tools"),
            ],
        ),
    ]

    with (
        patch("agent_app.tools.manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._get_node_version", new_callable=AsyncMock, return_value=None),
    ):
        status = await get_tool_status(adapter_registry=registry, desired_packs=desired)

    pack_tools = status["packs"]["test-pack"]
    go_ios_entry = next(e for e in pack_tools if e["name"] == "go_ios")
    xcode_entry = next(e for e in pack_tools if e["name"] == "xcodebuild")
    assert go_ios_entry["version"] is None
    assert xcode_entry["version"] is None


async def test_get_tool_status_no_packs_returns_empty_packs() -> None:
    with (
        patch("agent_app.tools.manager.detect_node_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._get_node_version", new_callable=AsyncMock, return_value=None),
    ):
        status = await get_tool_status(desired_packs=[])

    assert status["packs"] == {}
    assert "host" in status


async def test_detect_fnm_provider_fallback_bin_dirs() -> None:
    with (
        patch("agent_app.tools.manager._find_fnm_binary", return_value="/fnm"),
        patch("agent_app.tools.manager._run_optional", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._fnm_default_bin_dirs", return_value=["/fnm/aliases/default/bin"]),
        patch(
            "agent_app.tools.manager._is_executable",
            side_effect=lambda p: p in {"/fnm/aliases/default/bin/node", "/fnm/aliases/default/bin/npm"},
        ),
    ):
        provider = await _detect_fnm_provider()

    assert provider == NodeProvider(
        name="fnm",
        node_path="/fnm/aliases/default/bin/node",
        npm_path="/fnm/aliases/default/bin/npm",
        bin_paths=["/fnm/aliases/default/bin"],
    )


def test_detect_nvm_provider_no_candidates() -> None:
    with patch("agent_app.tools.manager.glob.glob", return_value=[]):
        assert _detect_nvm_provider() is None


def test_detect_nvm_provider_npm_not_executable() -> None:
    with (
        patch(
            "agent_app.tools.manager.glob.glob",
            return_value=["/nvm/versions/node/v20.0.0/bin/node"],
        ),
        patch(
            "agent_app.tools.manager._is_executable",
            side_effect=lambda p: p.endswith("/node"),
        ),
    ):
        assert _detect_nvm_provider() is None


def test_detect_system_provider_which_finds_both() -> None:
    with patch("agent_app.tools.manager.shutil.which", side_effect=["/usr/bin/node", "/usr/bin/npm"]):
        provider = _detect_system_provider()

    assert provider == NodeProvider(
        name="system",
        node_path="/usr/bin/node",
        npm_path="/usr/bin/npm",
        bin_paths=["/usr/bin"],
    )


def test_detect_system_provider_which_none_fallback_local_bin() -> None:
    with (
        patch("agent_app.tools.manager.shutil.which", return_value=None),
        patch(
            "agent_app.tools.manager._is_executable",
            side_effect=lambda p: p in {"/usr/local/bin/node", "/usr/local/bin/npm"},
        ),
    ):
        provider = _detect_system_provider()

    assert provider is not None
    assert provider.node_path == "/usr/local/bin/node"
    assert provider.npm_path == "/usr/local/bin/npm"


def test_detect_system_provider_which_none_fallback_usr_bin() -> None:
    with (
        patch("agent_app.tools.manager.shutil.which", return_value=None),
        patch(
            "agent_app.tools.manager._is_executable",
            side_effect=lambda p: p in {"/usr/bin/node", "/usr/bin/npm"},
        ),
    ):
        provider = _detect_system_provider()

    assert provider is not None
    assert provider.node_path == "/usr/bin/node"
    assert provider.npm_path == "/usr/bin/npm"


def test_detect_system_provider_nothing_found() -> None:
    with (
        patch("agent_app.tools.manager.shutil.which", return_value=None),
        patch("agent_app.tools.manager._is_executable", return_value=False),
    ):
        assert _detect_system_provider() is None


async def test_detect_node_provider_returns_nvm_when_fnm_missing() -> None:
    nvm = NodeProvider(name="nvm", node_path="/nvm/node", npm_path="/nvm/npm")
    with (
        patch("agent_app.tools.manager._detect_fnm_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._detect_nvm_provider", return_value=nvm),
    ):
        provider = await detect_node_provider()

    assert provider == nvm


async def test_detect_node_provider_returns_system_when_others_missing() -> None:
    system = NodeProvider(name="system", node_path="/usr/bin/node", npm_path="/usr/bin/npm")
    with (
        patch("agent_app.tools.manager._detect_fnm_provider", new_callable=AsyncMock, return_value=None),
        patch("agent_app.tools.manager._detect_nvm_provider", return_value=None),
        patch("agent_app.tools.manager._detect_system_provider", return_value=system),
    ):
        provider = await detect_node_provider()

    assert provider == system


async def test_get_node_version_none_when_provider_has_error() -> None:
    provider = NodeProvider(name="fnm", node_path=None, npm_path=None, error="node_not_configured")
    assert await _get_node_version(provider) is None


async def test_get_node_version_none_when_run_fails() -> None:
    provider = NodeProvider(name="system", node_path="/usr/bin/node", npm_path="/usr/bin/npm")
    with patch(
        "agent_app.tools.manager._run_optional",
        new_callable=AsyncMock,
        return_value=CommandResult(1, "err"),
    ):
        assert await _get_node_version(provider) is None
