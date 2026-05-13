from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models.driver_pack import DriverPack, DriverPackFeature, DriverPackPlatform, DriverPackRelease, PackState
from app.services import pack_service


class ScalarRowsResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self) -> ScalarRowsResult:
        return self

    def all(self) -> list[object]:
        return self.rows

    def scalar_one_or_none(self) -> object | None:
        return self.rows[0] if self.rows else None


class ExecuteSession:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.committed = False

    async def execute(self, *_args: object, **_kwargs: object) -> object:
        return self.results.pop(0)

    async def commit(self) -> None:
        self.committed = True


class ScalarSession:
    def __init__(self, value: object | None) -> None:
        self.value = value

    async def scalar(self, *_args: object, **_kwargs: object) -> object | None:
        return self.value


def _platform() -> DriverPackPlatform:
    return DriverPackPlatform(
        manifest_platform_id="android",
        display_name="Android",
        automation_name="uiautomator2",
        appium_platform_name="Android",
        device_types=["real_device"],
        connection_types=["usb"],
        grid_slots=["default"],
        data={
            "identity": {"scheme": "serial", "scope": "host"},
            "lifecycle_actions": [{"id": "reconnect"}],
            "health_checks": [{"id": "adb"}],
            "device_fields_schema": [{"name": "serial"}],
            "capabilities": {"platformName": "Android"},
            "display": {"icon": "phone"},
            "default_capabilities": {"automationName": "UiAutomator2"},
            "connection_behavior": {"requires_host": True},
            "parallel_resources": {"usb": 1},
            "device_type_overrides": {"emulator": {"grid_slots": ["emulator"]}},
        },
    )


def _release() -> DriverPackRelease:
    release = DriverPackRelease(
        release="1.0.0",
        manifest_json={
            "appium_server": {
                "source": "npm",
                "package": "appium",
                "version": "2.0.0",
                "recommended": "2.0.1",
                "known_bad": [1],
                "github_repo": "appium/appium",
            },
            "appium_driver": {"source": "npm", "package": "driver", "version": "3.0.0"},
            "workarounds": [{"id": "wda", "applies_when": {"platform": "ios"}, "env": {"A": 1}}, "skip"],
            "doctor": [{"id": "adb", "description": "ADB", "adapter_hook": "doctor_adb"}, "skip"],
            "insecure_features": ["adb_shell"],
        },
        derived_from_pack_id="source",
        derived_from_release="0.9.0",
        platforms=[_platform()],
        features=[
            DriverPackFeature(
                manifest_feature_id="screen-record",
                data={
                    "display_name": "Screen Record",
                    "description_md": "Record screen",
                    "actions": [{"id": "start"}, {"id": "stop", "label": "Stop"}],
                },
            )
        ],
    )
    return release


def _pack(state: PackState = PackState.enabled) -> DriverPack:
    return DriverPack(
        id="local/pack",
        origin="uploaded",
        display_name="Pack",
        maintainer="GridFleet",
        license="Apache-2.0",
        current_release="1.0.0",
        state=state,
        runtime_policy={"strategy": "recommended"},
        releases=[_release()],
    )


def test_pack_service_builds_pack_outputs_from_manifest_helpers() -> None:
    pack = _pack()
    out = pack_service.build_pack_out(pack)

    assert out.id == "local/pack"
    assert out.current_release == "1.0.0"
    assert out.derived_from is not None
    assert out.derived_from.pack_id == "source"
    assert out.appium_server is not None
    assert out.appium_server.known_bad == ["1"]
    assert out.appium_driver is not None
    assert out.workarounds[0].env == {"A": "1"}
    assert out.doctor[0].adapter_hook == "doctor_adb"
    assert out.platforms[0].identity_scheme == "serial"
    assert out.features["screen-record"].actions[1].label == "Stop"

    empty_pack = DriverPack(
        id="local/empty",
        origin="uploaded",
        display_name="Empty",
        maintainer="",
        license="",
        state=PackState.enabled,
        runtime_policy=None,  # type: ignore[arg-type]
        releases=[],
    )
    empty = pack_service.build_pack_out(empty_pack)
    assert empty.current_release is None
    assert empty.platforms == []


def test_pack_service_helper_branches_handle_empty_and_nested_values() -> None:
    release = DriverPackRelease(release="1.0.0", manifest_json={})
    assert pack_service._derived_from(None) is None
    assert pack_service._derived_from(release) is None
    assert pack_service._installable_out(None) is None
    assert pack_service._workarounds_out({"id": "bad"}) == []
    assert pack_service._doctor_out({"id": "bad"}) == []

    direct = SimpleNamespace(resolved_install_spec={"appium_driver_version": 3})
    nested = SimpleNamespace(resolved_install_spec={"appium_driver": {"uiautomator2": "4.0.0"}})
    missing = SimpleNamespace(resolved_install_spec={"appium_driver": {"uiautomator2": None}})
    assert pack_service._desired_driver_version(direct) == "3"
    assert pack_service._desired_driver_version(nested) == "4.0.0"
    assert pack_service._desired_driver_version(missing) is None
    assert pack_service._desired_driver_version(SimpleNamespace(resolved_install_spec=None)) is None

    assert pack_service._runtime_driver_version(SimpleNamespace(driver_specs=[{"version": 5}])) == "5"
    assert pack_service._runtime_driver_version(SimpleNamespace(driver_specs=[])) is None


async def test_runtime_summaries_count_hosts_versions_and_driver_drift() -> None:
    installed = SimpleNamespace(
        pack_id="local/pack",
        status="installed",
        resolved_install_spec={"appium_driver_version": "3.0.0"},
    )
    blocked = SimpleNamespace(
        pack_id="local/pack",
        status="blocked",
        resolved_install_spec={"appium_driver": {"uiautomator2": "4.0.0"}},
    )
    runtime = SimpleNamespace(appium_server_version="2.0.0", driver_specs=[{"version": "3.1.0"}])
    session = ExecuteSession(SimpleNamespace(all=lambda: [(installed, runtime), (blocked, None)]))

    summaries = await pack_service._runtime_summaries_by_pack(session, ["local/pack"])

    summary = summaries["local/pack"]
    assert summary.installed_hosts == 1
    assert summary.blocked_hosts == 1
    assert summary.actual_appium_server_versions == ["2.0.0"]
    assert summary.actual_appium_driver_versions == ["3.1.0"]
    assert summary.driver_drift_hosts == 1
    assert await pack_service._runtime_summaries_by_pack(session, []) == {}


async def test_pack_catalog_and_detail_use_runtime_summaries_and_drain_counts() -> None:
    pack = _pack(PackState.draining)
    session = ExecuteSession(ScalarRowsResult([pack]), SimpleNamespace(all=lambda: []))

    with (
        patch("app.services.pack_service.try_complete_drain", new=AsyncMock()) as complete_drain,
        patch(
            "app.services.pack_service.count_active_work_for_pack",
            new=AsyncMock(return_value={"active_runs": 2, "live_sessions": 1}),
        ),
    ):
        catalog = await pack_service.list_catalog(session)  # type: ignore[arg-type]

    assert session.committed is True
    complete_drain.assert_awaited_once_with(session, "local/pack")
    assert catalog.packs[0].active_runs == 2
    assert catalog.packs[0].live_sessions == 1

    missing = await pack_service.get_pack_detail(ExecuteSession(ScalarRowsResult([])), "missing")  # type: ignore[arg-type]
    assert missing is None

    detail_session = ExecuteSession(ScalarRowsResult([pack]), SimpleNamespace(all=lambda: []))
    detail = await pack_service.get_pack_detail(detail_session, "local/pack")  # type: ignore[arg-type]
    assert detail is not None
    assert detail.id == "local/pack"


async def test_pack_platforms_returns_missing_empty_and_selected_release() -> None:
    assert await pack_service.get_platforms(ScalarSession(None), "missing") is None  # type: ignore[arg-type]

    empty_pack = DriverPack(
        id="local/empty",
        origin="uploaded",
        display_name="Empty",
        maintainer="",
        license="",
        state=PackState.enabled,
        runtime_policy={"strategy": "recommended"},
        releases=[],
    )
    assert await pack_service.get_platforms(ScalarSession(empty_pack), "local/empty") is None  # type: ignore[arg-type]

    platforms = await pack_service.get_platforms(ScalarSession(_pack()), "local/pack")  # type: ignore[arg-type]
    assert platforms is not None
    assert platforms.release == "1.0.0"
    assert platforms.platforms[0].id == "android"
