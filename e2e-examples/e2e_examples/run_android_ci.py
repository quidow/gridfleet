"""Reserve Android devices, prepare them, run Appium tests, and always release the run."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from gridfleet_testkit import GridFleetClient, register_run_cleanup

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_ANDROID_APK_URL = (
    "https://github.com/appium/appium/raw/master/packages/appium/sample-code/apps/ApiDemos-debug.apk"
)
DEFAULT_ANDROID_PACK_ID = "appium-uiautomator2"
DEFAULT_ANDROID_PLATFORM = "android_mobile"
DEFAULT_ANDROID_DEVICE_COUNT = 1
DEFAULT_HEARTBEAT_INTERVAL_SEC = 30
DEFAULT_HEARTBEAT_TIMEOUT_SEC = 120
DEFAULT_TTL_MINUTES = 45
DEFAULT_PYTEST_ARGS = ("tests/test_android_e2e.py", "-m", "e2e_hardware", "-q")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AndroidCIConfig:
    gridfleet_url: str
    gridfleet_api_url: str
    grid_url: str
    run_name: str
    created_by: str
    android_pack_id: str = DEFAULT_ANDROID_PACK_ID
    android_platform: str = DEFAULT_ANDROID_PLATFORM
    android_device_count: int = DEFAULT_ANDROID_DEVICE_COUNT
    android_apk_url: str = DEFAULT_ANDROID_APK_URL
    adb_path: str = "adb"
    heartbeat_interval_sec: int = DEFAULT_HEARTBEAT_INTERVAL_SEC
    heartbeat_timeout_sec: int = DEFAULT_HEARTBEAT_TIMEOUT_SEC
    ttl_minutes: int = DEFAULT_TTL_MINUTES
    pytest_args: tuple[str, ...] = DEFAULT_PYTEST_ARGS
    junit_xml_path: Path | None = None


def normalize_gridfleet_api_url(gridfleet_url: str) -> str:
    normalized = gridfleet_url.rstrip("/")
    if normalized.endswith("/api"):
        return normalized
    return f"{normalized}/api"


def _require_env(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {key}")
    return value


def _default_run_name(env: Mapping[str, str]) -> str:
    if github_run_id := env.get("GITHUB_RUN_ID"):
        return f"android-e2e-{github_run_id}"
    return "android-e2e-local"


def _default_created_by(env: Mapping[str, str]) -> str:
    if github_repository := env.get("GITHUB_REPOSITORY"):
        workflow = env.get("GITHUB_WORKFLOW", "manual")
        run_id = env.get("GITHUB_RUN_ID", "local")
        return f"github/{github_repository}/{workflow}/{run_id}"
    return "local/e2e-examples"


def _load_pytest_args(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("ANDROID_PYTEST_ARGS", "").strip()
    if not raw:
        return DEFAULT_PYTEST_ARGS
    parsed = tuple(shlex.split(raw))
    if not parsed:
        raise ValueError("ANDROID_PYTEST_ARGS must not be empty")
    return parsed


def load_config(env: Mapping[str, str] | None = None) -> AndroidCIConfig:
    data = env or os.environ
    gridfleet_url = _require_env(data, "GRIDFLEET_URL")
    grid_url = _require_env(data, "GRID_URL")
    gridfleet_api_url = data.get("GRIDFLEET_API_URL", "").strip() or normalize_gridfleet_api_url(gridfleet_url)
    android_device_count = int(data.get("ANDROID_DEVICE_COUNT", str(DEFAULT_ANDROID_DEVICE_COUNT)))
    if android_device_count < 1:
        raise ValueError("ANDROID_DEVICE_COUNT must be at least 1")

    return AndroidCIConfig(
        gridfleet_url=gridfleet_url.rstrip("/"),
        gridfleet_api_url=gridfleet_api_url.rstrip("/"),
        grid_url=grid_url.rstrip("/"),
        run_name=data.get("ANDROID_RUN_NAME", "").strip() or _default_run_name(data),
        created_by=data.get("ANDROID_CREATED_BY", "").strip() or _default_created_by(data),
        android_pack_id=data.get("ANDROID_PACK_ID", DEFAULT_ANDROID_PACK_ID).strip() or DEFAULT_ANDROID_PACK_ID,
        android_platform=data.get("ANDROID_PLATFORM", DEFAULT_ANDROID_PLATFORM).strip() or DEFAULT_ANDROID_PLATFORM,
        android_device_count=android_device_count,
        android_apk_url=data.get("ANDROID_APK_URL", DEFAULT_ANDROID_APK_URL).strip() or DEFAULT_ANDROID_APK_URL,
        adb_path=data.get("ANDROID_ADB_PATH", "adb").strip() or "adb",
        pytest_args=_load_pytest_args(data),
        junit_xml_path=(
            Path(raw_junit_path) if (raw_junit_path := data.get("ANDROID_JUNIT_XML", "").strip()) else None
        ),
    )


def _require_string_field(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Run payload is missing a valid {key!r} value")
    return value


def download_apk(apk_url: str) -> Path:
    response = httpx.get(apk_url, follow_redirects=True, timeout=60)
    response.raise_for_status()
    suffix = Path(urlsplit(apk_url).path).suffix or ".apk"
    with tempfile.NamedTemporaryFile(prefix="gridfleet-e2e-", suffix=suffix, delete=False) as file_handle:
        file_handle.write(response.content)
        return Path(file_handle.name)


def install_apk_on_device(adb_path: str, connection_target: str, apk_path: Path) -> None:
    try:
        subprocess.run(
            [adb_path, "-s", connection_target, "install", "-r", str(apk_path)],
            capture_output=True,
            check=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(detail) from exc


def resolve_adb_target(
    client: GridFleetClient,
    device_id: str,
    fallback_target: str,
) -> str:
    try:
        capabilities = client.get_device_capabilities(device_id)
    except Exception as exc:
        logger.warning(
            "Falling back to reserved connection target %s for device %s because capability lookup failed: %s",
            fallback_target,
            device_id,
            exc,
        )
        return fallback_target

    runtime_target = capabilities.get("appium:udid")
    if isinstance(runtime_target, str) and runtime_target.strip():
        return runtime_target
    return fallback_target


def prepare_devices(
    client: GridFleetClient,
    run_id: str,
    devices: list[dict[str, Any]],
    apk_path: Path,
    adb_path: str,
) -> list[dict[str, Any]]:
    prepared_devices: list[dict[str, Any]] = []
    for device in devices:
        device_id = _require_string_field(device, "device_id")
        connection_target = _require_string_field(device, "connection_target")
        adb_target = resolve_adb_target(client, device_id, connection_target)
        try:
            install_apk_on_device(adb_path, adb_target, apk_path)
        except Exception as exc:
            target_label = (
                connection_target if adb_target == connection_target else f"{connection_target} via {adb_target}"
            )
            client.report_preparation_failure(
                run_id,
                device_id,
                f"APK install failed for {target_label}: {exc}",
                source="ci_preparation",
            )
        else:
            prepared_devices.append(device)
    return prepared_devices


def build_pytest_env(config: AndroidCIConfig) -> dict[str, str]:
    env = dict(os.environ)
    env["GRIDFLEET_API_URL"] = config.gridfleet_api_url
    env["GRID_URL"] = config.grid_url
    env["GRIDFLEET_TESTKIT_PACK_ID"] = config.android_pack_id
    env["GRIDFLEET_TESTKIT_PLATFORM_ID"] = config.android_platform
    return env


def run_pytest_suite(config: AndroidCIConfig) -> int:
    cwd = Path(__file__).resolve().parents[1]
    command = [sys.executable, "-m", "pytest", *config.pytest_args]
    if config.junit_xml_path is not None:
        junit_xml_path = config.junit_xml_path if config.junit_xml_path.is_absolute() else cwd / config.junit_xml_path
        junit_xml_path.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--junitxml", str(junit_xml_path)])
    result = subprocess.run(command, cwd=cwd, env=build_pytest_env(config), check=False)
    return result.returncode


def finalize_run(client: GridFleetClient, run_id: str, prefer_complete: bool) -> None:
    if prefer_complete:
        try:
            client.complete_run(run_id)
            return
        except Exception:
            logger.exception("Completing run %s failed; falling back to cancel", run_id)
    client.cancel_run(run_id)


def run(config: AndroidCIConfig) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = GridFleetClient(config.gridfleet_api_url)
    run_id: str | None = None
    heartbeat_thread: Any = None
    apk_path: Path | None = None
    prefer_complete = False

    try:
        run_payload = client.reserve_devices(
            name=config.run_name,
            requirements=[
                {
                    "pack_id": config.android_pack_id,
                    "platform_id": config.android_platform,
                    "count": config.android_device_count,
                }
            ],
            ttl_minutes=config.ttl_minutes,
            heartbeat_timeout_sec=config.heartbeat_timeout_sec,
            created_by=config.created_by,
        )
        run_id = _require_string_field(run_payload, "id")
        devices = run_payload.get("devices")
        if not isinstance(devices, list):
            raise ValueError("Run payload is missing a valid devices list")

        heartbeat_thread = client.start_heartbeat(run_id, interval=config.heartbeat_interval_sec)
        register_run_cleanup(client, run_id, heartbeat_thread)

        apk_path = download_apk(config.android_apk_url)
        prepared_devices = prepare_devices(client, run_id, devices, apk_path, config.adb_path)
        if not prepared_devices:
            logger.error("No reserved devices survived Android preparation")
            return 1

        client.signal_ready(run_id)
        client.signal_active(run_id)
        prefer_complete = True
        return run_pytest_suite(config)
    finally:
        if apk_path is not None:
            apk_path.unlink(missing_ok=True)
        if heartbeat_thread is not None:
            heartbeat_thread.stop()
        if run_id is not None:
            finalize_run(client, run_id, prefer_complete=prefer_complete)


def main() -> int:
    return run(load_config())


if __name__ == "__main__":
    raise SystemExit(main())
