from agent_app.pack.manifest import AppiumInstallable
from agent_app.pack.runtime_policy import RuntimePolicy, resolve_runtime_spec


def _server() -> AppiumInstallable:
    return AppiumInstallable("npm", "appium", ">=2.5,<3", "2.11.5", [])


def _driver(known_bad: list[str] | None = None) -> AppiumInstallable:
    return AppiumInstallable("npm", "appium-uiautomator2-driver", ">=3,<4", "3.6.0", known_bad or [])


def test_recommended_uses_manifest_recommended() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(),
        policy=RuntimePolicy(strategy="recommended"),
    )

    assert spec.error is None
    assert spec.runtime_spec is not None
    assert spec.runtime_spec.server_version == "2.11.5"
    assert spec.runtime_spec.drivers[0][1] == "3.6.0"


def test_exact_uses_pinned_versions() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(),
        policy=RuntimePolicy(
            strategy="exact",
            appium_server_version="2.12.0",
            appium_driver_version="3.7.0",
        ),
    )

    assert spec.error is None
    assert spec.runtime_spec is not None
    assert spec.runtime_spec.server_version == "2.12.0"
    assert spec.runtime_spec.drivers[0][1] == "3.7.0"


def test_exact_rejects_known_bad_driver() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(known_bad=["3.7.0"]),
        policy=RuntimePolicy(
            strategy="exact",
            appium_server_version="2.12.0",
            appium_driver_version="3.7.0",
        ),
    )

    assert spec.runtime_spec is None
    assert spec.error == "pinned_version_unavailable:appium_driver_version=3.7.0 known_bad"


def test_exact_rejects_driver_outside_manifest_range() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(),
        policy=RuntimePolicy(
            strategy="exact",
            appium_server_version="2.12.0",
            appium_driver_version="4.1.0",
        ),
    )

    assert spec.runtime_spec is None
    assert spec.error == "pinned_version_unavailable:appium_driver_version=4.1.0 outside >=3,<4"


def test_latest_patch_uses_highest_patch_in_recommended_minor() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(known_bad=["3.6.2"]),
        policy=RuntimePolicy(strategy="latest_patch"),
        available_versions={
            "appium": ["2.11.4", "2.11.5", "2.11.9", "2.12.0"],
            "appium-uiautomator2-driver": ["3.5.9", "3.6.0", "3.6.2", "3.6.3", "3.7.0"],
        },
    )

    assert spec.error is None
    assert spec.runtime_spec is not None
    assert spec.runtime_spec.server_version == "2.11.9"
    assert spec.runtime_spec.drivers[0][1] == "3.6.3"


def test_latest_patch_rejects_missing_recommended() -> None:
    server = AppiumInstallable("npm", "appium", ">=2.5,<3", None, [])
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=server,
        appium_driver=_driver(),
        policy=RuntimePolicy(strategy="latest_patch"),
        available_versions={"appium": ["2.11.9"], "appium-uiautomator2-driver": ["3.6.3"]},
    )

    assert spec.runtime_spec is None
    assert spec.error == "pinned_version_unavailable:appium_server_version=recommended missing"


def test_latest_patch_rejects_non_npm_source() -> None:
    github_driver = AppiumInstallable(
        "github",
        "appium-roku-driver",
        ">=0.11,<0.12",
        "0.11.6",
        [],
        "dlenroc/appium-roku-driver",
    )
    spec = resolve_runtime_spec(
        pack_id="n-dlenroc",
        appium_server=_server(),
        appium_driver=github_driver,
        policy=RuntimePolicy(strategy="latest_patch"),
        available_versions={"appium": ["2.11.9"], "appium-roku-driver": ["0.11.6"]},
    )

    assert spec.runtime_spec is None
    assert spec.error == "version_resolution_unavailable:latest_patch_source=github"
