from agent_app.pack.manifest import AppiumInstallable
from agent_app.pack.runtime_policy import RuntimePolicy, resolve_runtime_spec


def _server(recommended: str | None = "2.11.5") -> AppiumInstallable:
    return AppiumInstallable("npm", "appium", ">=2.5,<3", recommended, [])


def _driver(known_bad: list[str] | None = None, recommended: str | None = "3.6.0") -> AppiumInstallable:
    return AppiumInstallable("npm", "appium-uiautomator2-driver", ">=3,<4", recommended, known_bad or [])


def test_recommended_server_rejects_known_bad() -> None:
    server = AppiumInstallable("npm", "appium", ">=2.5,<3", "2.11.5", known_bad=["2.11.5"])
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=server,
        appium_driver=_driver(),
        policy=RuntimePolicy(strategy="recommended"),
    )
    assert spec.runtime_spec is None
    assert "known_bad" in spec.error


def test_recommended_server_outside_range() -> None:
    server = AppiumInstallable("npm", "appium", ">=2.5,<2.6", "3.0.0", [])
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=server,
        appium_driver=_driver(),
        policy=RuntimePolicy(strategy="recommended"),
    )
    assert spec.runtime_spec is None
    assert "outside" in spec.error


def test_recommended_driver_rejects_known_bad() -> None:
    driver = _driver(known_bad=["3.6.0"])
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=driver,
        policy=RuntimePolicy(strategy="recommended"),
    )
    assert spec.runtime_spec is None
    assert "known_bad" in spec.error


def test_recommended_driver_outside_range() -> None:
    driver = AppiumInstallable("npm", "appium-uiautomator2-driver", ">=3,<3.1", "4.0.0", [])
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=driver,
        policy=RuntimePolicy(strategy="recommended"),
    )
    assert spec.runtime_spec is None
    assert "outside" in spec.error


def test_recommended_server_missing() -> None:
    server = _server(recommended=None)
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=server,
        appium_driver=_driver(),
        policy=RuntimePolicy(strategy="recommended"),
    )
    assert spec.runtime_spec is None
    assert "recommended missing" in spec.error


def test_recommended_driver_missing() -> None:
    driver = _driver(recommended=None)
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=driver,
        policy=RuntimePolicy(strategy="recommended"),
    )
    assert spec.runtime_spec is None
    assert "recommended missing" in spec.error


def test_latest_patch_invalid_version_skipped() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(),
        policy=RuntimePolicy(strategy="latest_patch"),
        available_versions={
            "appium": ["2.11.5", "not-a-version"],
            "appium-uiautomator2-driver": ["3.6.0"],
        },
    )
    assert spec.error is None
    assert spec.runtime_spec is not None
    assert spec.runtime_spec.server_version == "2.11.5"


def test_latest_patch_prerelease_skipped() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(),
        policy=RuntimePolicy(strategy="latest_patch"),
        available_versions={
            "appium": ["2.11.5", "2.11.6a1"],
            "appium-uiautomator2-driver": ["3.6.0"],
        },
    )
    assert spec.error is None
    assert spec.runtime_spec is not None
    assert spec.runtime_spec.server_version == "2.11.5"


def test_latest_patch_major_minor_mismatch_skipped() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(),
        policy=RuntimePolicy(strategy="latest_patch"),
        available_versions={
            "appium": ["2.11.5", "2.12.0"],
            "appium-uiautomator2-driver": ["3.6.0"],
        },
    )
    assert spec.error is None
    assert spec.runtime_spec is not None
    assert spec.runtime_spec.server_version == "2.11.5"


def test_latest_patch_not_in_specifier_skipped() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=AppiumInstallable("npm", "appium", ">=2.5,<2.6", "2.5.1", []),
        appium_driver=_driver(),
        policy=RuntimePolicy(strategy="latest_patch"),
        available_versions={
            "appium": ["2.5.1", "2.6.0"],
            "appium-uiautomator2-driver": ["3.6.0"],
        },
    )
    assert spec.error is None
    assert spec.runtime_spec is not None
    assert spec.runtime_spec.server_version == "2.5.1"


def test_latest_patch_known_bad_skipped() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(known_bad=["3.6.0"]),
        policy=RuntimePolicy(strategy="latest_patch"),
        available_versions={
            "appium": ["2.11.5"],
            "appium-uiautomator2-driver": ["3.5.9", "3.6.0", "3.6.1"],
        },
    )
    assert spec.error is None
    assert spec.runtime_spec is not None
    assert spec.runtime_spec.drivers[0][1] == "3.6.1"


def test_latest_patch_no_candidates() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(),
        policy=RuntimePolicy(strategy="latest_patch"),
        available_versions={
            "appium": ["2.12.0"],  # major.minor mismatch
            "appium-uiautomator2-driver": ["4.0.0"],  # major.minor mismatch
        },
    )
    assert spec.runtime_spec is None
    assert "no candidates" in spec.error


def test_exact_missing_server_pin() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(),
        policy=RuntimePolicy(strategy="exact", appium_server_version=None, appium_driver_version="3.6.0"),
    )
    assert spec.runtime_spec is None
    assert "exact pins missing" in spec.error


def test_exact_missing_driver_pin() -> None:
    spec = resolve_runtime_spec(
        pack_id="appium-uiautomator2",
        appium_server=_server(),
        appium_driver=_driver(),
        policy=RuntimePolicy(strategy="exact", appium_server_version="2.11.5", appium_driver_version=None),
    )
    assert spec.runtime_spec is None
    assert "exact pins missing" in spec.error
