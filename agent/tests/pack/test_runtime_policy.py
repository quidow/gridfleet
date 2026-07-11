from agent_app.pack.manifest import AppiumInstallable
from agent_app.pack.runtime_policy import resolve_runtime_spec


def _server(recommended: str | None = "2.11.5") -> AppiumInstallable:
    return AppiumInstallable("npm", "appium", ">=2.5,<3", recommended, [])


def _driver(known_bad: list[str] | None = None) -> AppiumInstallable:
    return AppiumInstallable("npm", "appium-uiautomator2-driver", ">=3,<4", "3.6.0", known_bad or [])


def test_recommended_uses_manifest_recommended() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(),
    )

    assert spec.error is None
    assert spec.runtime_spec is not None
    assert spec.runtime_spec.server_version == "2.11.5"
    assert spec.runtime_spec.drivers[0][1] == "3.6.0"


def test_recommended_rejects_missing_recommended() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(recommended=None),
        appium_driver=_driver(),
    )

    assert spec.runtime_spec is None
    assert spec.error == "pinned_version_unavailable:appium_server_version=recommended missing"


def test_recommended_rejects_known_bad_driver() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(known_bad=["3.6.0"]),
    )

    assert spec.runtime_spec is None
    assert spec.error == "pinned_version_unavailable:appium_driver_version=3.6.0 known_bad"
