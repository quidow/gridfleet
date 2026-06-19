"""Public GridFleet client helpers for external test suites."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Literal, TypedDict, cast

import httpx2 as httpx

from .errors import ReserveCapabilitiesUnsupportedError, UnknownIncludeError
from .run_lifecycle import HeartbeatThread

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .types import JsonObject, JsonObjectList, QueryParamValue

DEFAULT_GRID_URL = "http://localhost:4444"
DEFAULT_GRIDFLEET_API_URL = "http://localhost:8000/api"

logger = logging.getLogger("gridfleet_testkit")


def _default_grid_url() -> str:
    return os.getenv("GRID_URL", DEFAULT_GRID_URL)


def run_grid_url(run_id: str, *, base: str | None = None) -> str:
    """Run-scoped WebDriver endpoint for *run_id* (``{base}/run/{run_id}``).

    Sessions created through it are admitted only to devices reserved for the
    run; free sessions use the bare grid URL. Replaces the retired
    ``gridfleet:run_id`` capability.
    """
    root = (base or _default_grid_url()).rstrip("/")
    return f"{root}/run/{run_id}"


def _default_api_url() -> str:
    return os.getenv("GRIDFLEET_API_URL", DEFAULT_GRIDFLEET_API_URL)


def __getattr__(name: str) -> str:
    if name == "GRID_URL":
        return _default_grid_url()
    if name == "GRIDFLEET_API_URL":
        return _default_api_url()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _default_auth() -> httpx.BasicAuth | None:
    """Build httpx Basic auth from env vars, or return None when unset."""
    username = os.getenv("GRIDFLEET_TESTKIT_USERNAME")
    password = os.getenv("GRIDFLEET_TESTKIT_PASSWORD")
    if not username or not password:
        return None
    return httpx.BasicAuth(username, password)


def _raise_for_status(resp: httpx.Response, *, run_id: str) -> None:
    del run_id
    if resp.status_code == 422:
        try:
            payload = resp.json()
        except Exception:
            payload = None
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            details = error.get("details")
            if isinstance(details, dict):
                code = details.get("code")
                if code == "unknown_include":
                    values = details.get("values")
                    raise UnknownIncludeError(values if isinstance(values, list) else [])
                if code == "reserve_capabilities_unsupported":
                    raise ReserveCapabilitiesUnsupportedError(str(error.get("message") or ""))
    resp.raise_for_status()


def _query_params(values: dict[str, QueryParamValue]) -> list[tuple[str, QueryParamValue]]:
    params: list[tuple[str, QueryParamValue]] = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            params.append((key, str(value).lower()))
        else:
            params.append((key, str(value)))
    return params


def _normalize_include(include: Sequence[str] | None) -> tuple[str, ...] | None:
    if include is None:
        return None
    if isinstance(include, (str, bytes)):
        raise TypeError(
            "include must be a sequence of strings, not a string itself "
            "(e.g. include=('config',), not include='config')"
        )
    return tuple(include)


def _include_param(include: tuple[str, ...] | None) -> list[tuple[str, QueryParamValue]] | None:
    if include is None:
        return None
    values = [v for v in include if v]
    if not values:
        return None
    return [("include", ",".join(values))]


def _raise_or_warn(operation: str, suppress_errors: bool, exc: Exception) -> None:
    if not suppress_errors:
        raise exc
    logger.warning("Failed to %s with GridFleet: %s", operation, exc)


class CooldownSetResult(TypedDict):
    status: Literal["cooldown_set"]
    excluded_until: str
    cooldown_count: int


class CooldownEscalatedResult(TypedDict):
    status: Literal["maintenance_escalated"]
    cooldown_count: int
    threshold: int


CooldownResult = CooldownSetResult | CooldownEscalatedResult


class GridFleetClient:
    """Client for the GridFleet API, used by test fixtures and CI flows."""

    def __init__(
        self,
        base_url: str | None = None,
        auth: httpx.BasicAuth | None = None,
    ):
        self.base_url = (base_url or _default_api_url()).rstrip("/")
        self._auth = auth if auth is not None else _default_auth()

    def list_devices(
        self,
        *,
        pack_id: str | None = None,
        platform_id: str | None = None,
        status: str | None = None,
        reserved: bool | None = None,
        host_id: str | None = None,
        identity_value: str | None = None,
        connection_target: str | None = None,
        device_type: str | None = None,
        connection_type: str | None = None,
        os_version: str | None = None,
        search: str | None = None,
        hardware_health_status: str | None = None,
        hardware_telemetry_state: str | None = None,
        needs_attention: bool | None = None,
        tags: dict[str, str] | None = None,
    ) -> JsonObjectList:
        """List devices with backend filter passthrough."""
        params = _query_params(
            {
                "pack_id": pack_id,
                "platform_id": platform_id,
                "status": status,
                "reserved": reserved,
                "host_id": host_id,
                "identity_value": identity_value,
                "connection_target": connection_target,
                "device_type": device_type,
                "connection_type": connection_type,
                "os_version": os_version,
                "search": search,
                "hardware_health_status": hardware_health_status,
                "hardware_telemetry_state": hardware_telemetry_state,
                "needs_attention": needs_attention,
            }
        )
        if tags:
            params.extend((f"tags.{key}", value) for key, value in tags.items())
        resp = httpx.get(
            f"{self.base_url}/devices",
            params=params,
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return cast("JsonObjectList", payload["items"])
        return cast("JsonObjectList", payload)

    def get_device(self, device_id: str) -> JsonObject:
        """Fetch one device detail row by backend device id."""
        resp = httpx.get(
            f"{self.base_url}/devices/{device_id}",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("JsonObject", resp.json())

    def get_device_config(self, device_id: str) -> JsonObject:
        """Fetch device config by backend device id."""
        resp = httpx.get(
            f"{self.base_url}/devices/{device_id}/config",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("JsonObject", resp.json())

    def get_device_capabilities(self, device_id: str) -> JsonObject:
        """Fetch the current Appium capabilities for a specific device."""
        resp = httpx.get(
            f"{self.base_url}/devices/{device_id}/capabilities",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("JsonObject", resp.json())

    def get_device_test_data(self, device_id: str) -> JsonObject:
        """Fetch operator-attached free-form test data for a specific device."""
        resp = httpx.get(
            f"{self.base_url}/devices/{device_id}/test_data",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("JsonObject", resp.json())

    def get_run(self, run_id: str) -> JsonObject:
        """Fetch one run detail row by backend run id."""
        resp = httpx.get(
            f"{self.base_url}/runs/{run_id}",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("JsonObject", resp.json())

    def replace_device_test_data(self, device_id: str, body: JsonObject) -> JsonObject:
        """Replace device test_data with the supplied object."""
        resp = httpx.put(
            f"{self.base_url}/devices/{device_id}/test_data",
            json=body,
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("JsonObject", resp.json())

    def merge_device_test_data(self, device_id: str, body: JsonObject) -> JsonObject:
        """Deep-merge into device test_data."""
        resp = httpx.patch(
            f"{self.base_url}/devices/{device_id}/test_data",
            json=body,
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("JsonObject", resp.json())

    def get_driver_pack_catalog(self) -> JsonObject:
        """Fetch enabled driver pack catalog data used for Appium platform selection."""
        resp = httpx.get(
            f"{self.base_url}/driver-packs/catalog",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("JsonObject", resp.json())

    def reserve_devices(
        self,
        name: str,
        requirements: JsonObjectList,
        ttl_minutes: int = 60,
        heartbeat_timeout_sec: int = 120,
        created_by: str | None = None,
        *,
        include: Sequence[str] | None = None,
    ) -> JsonObject:
        """Reserve devices for a test run and return the manager response."""
        include_tuple = _normalize_include(include)
        if include_tuple is not None and "capabilities" in include_tuple:
            raise ReserveCapabilitiesUnsupportedError("include='capabilities' is not supported on reserve")
        resp = httpx.post(
            f"{self.base_url}/runs",
            json={
                "name": name,
                "requirements": requirements,
                "ttl_minutes": ttl_minutes,
                "heartbeat_timeout_sec": heartbeat_timeout_sec,
                "created_by": created_by,
            },
            params=_include_param(include_tuple),
            timeout=30,
            auth=self._auth,
        )
        _raise_for_status(resp, run_id="")
        return cast("JsonObject", resp.json())

    def signal_ready(self, run_id: str) -> JsonObject:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/ready",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("JsonObject", resp.json())

    def signal_active(self, run_id: str) -> JsonObject:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/active",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("JsonObject", resp.json())

    def heartbeat(self, run_id: str) -> JsonObject:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/heartbeat",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("JsonObject", resp.json())

    def report_preparation_failure(
        self,
        run_id: str,
        device_id: str,
        message: str,
        source: str = "ci_preparation",
        *,
        suppress_errors: bool = False,
    ) -> JsonObject | None:
        try:
            resp = httpx.post(
                f"{self.base_url}/runs/{run_id}/devices/{device_id}/preparation-failed",
                json={"message": message, "source": source},
                timeout=10,
                auth=self._auth,
            )
            resp.raise_for_status()
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            _raise_or_warn("report preparation failure", suppress_errors, exc)
            return None
        return cast("JsonObject", resp.json())

    def update_session_status(
        self,
        session_id: str,
        status: str,
        *,
        suppress_errors: bool = True,
    ) -> JsonObject | None:
        """Update a registered session status."""
        try:
            resp = httpx.patch(
                f"{self.base_url}/sessions/{session_id}/status",
                json={"status": status},
                timeout=5,
                auth=self._auth,
            )
            resp.raise_for_status()
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            _raise_or_warn("report session status", suppress_errors, exc)
            return None
        return cast("JsonObject", resp.json())

    def complete_run(self, run_id: str) -> JsonObject:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/complete",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("JsonObject", resp.json())

    def cancel_run(self, run_id: str) -> JsonObject:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/cancel",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("JsonObject", resp.json())

    def cooldown_device(
        self,
        run_id: str,
        device_id: str,
        *,
        reason: str,
        ttl_seconds: int,
    ) -> CooldownResult:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/devices/{device_id}/cooldown",
            json={"reason": reason, "ttl_seconds": ttl_seconds},
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("CooldownResult", resp.json())

    def start_heartbeat(self, run_id: str, interval: int = 30) -> HeartbeatThread:
        thread = HeartbeatThread(self.base_url, run_id, interval, auth=self._auth)
        thread.start()
        return thread
