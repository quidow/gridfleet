"""Public GridFleet client helpers for external test suites."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import httpx2 as httpx

from . import config
from .device import Device
from .run_lifecycle import HeartbeatThread

if TYPE_CHECKING:
    from collections.abc import Callable

    from .types import (
        CooldownResult,
        JsonObject,
        JsonObjectList,
        JsonValue,
        QueryParamValue,
    )

logger = logging.getLogger("gridfleet_testkit")


def _raise_plain(resp: httpx.Response) -> None:
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


def _raise_or_warn(operation: str, suppress_errors: bool, exc: Exception) -> None:
    if not suppress_errors:
        raise exc
    logger.warning("Failed to %s with GridFleet: %s", operation, exc)


class GridFleetClient:
    """Client for the GridFleet API, used by test fixtures and CI flows."""

    def __init__(
        self,
        base_url: str | None = None,
        auth: httpx.BasicAuth | None = None,
    ):
        self.base_url = (base_url or config.api_url()).rstrip("/")
        self._auth = auth if auth is not None else config.auth_from_env()

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: JsonObject | None = None,
        params: list[tuple[str, QueryParamValue]] | None = None,
        timeout: float = 10,
        check: Callable[[httpx.Response], None] = _raise_plain,
    ) -> httpx.Response:
        resp = httpx.request(
            method,
            f"{self.base_url}{path}",
            json=json,
            params=params,
            timeout=timeout,
            auth=self._auth,
        )
        check(resp)
        return resp

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
    ) -> list[Device]:
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
        payload = self._send("GET", "/devices", params=params).json()
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            rows = cast("JsonObjectList", payload["items"])
        else:
            rows = cast("JsonObjectList", payload)
        return [Device.from_payload(row) for row in rows]

    def get_device(self, device_id: str) -> Device:
        """Fetch one device by backend device id, parsed into a typed ``Device``."""
        return Device.from_payload(cast("JsonObject", self._send("GET", f"/devices/{device_id}").json()))

    def get_device_test_data(self, device_id: str) -> JsonObject:
        """Fetch operator-attached free-form test data for a specific device."""
        return cast("JsonObject", self._send("GET", f"/devices/{device_id}/test_data").json())

    def get_run(self, run_id: str) -> JsonObject:
        """Fetch one run detail row by backend run id."""
        return cast("JsonObject", self._send("GET", f"/runs/{run_id}").json())

    def replace_device_test_data(self, device_id: str, body: JsonObject) -> JsonObject:
        """Replace device test_data with the supplied object."""
        return cast("JsonObject", self._send("PUT", f"/devices/{device_id}/test_data", json=body).json())

    def merge_device_test_data(self, device_id: str, body: JsonObject) -> JsonObject:
        """Deep-merge into device test_data."""
        return cast("JsonObject", self._send("PATCH", f"/devices/{device_id}/test_data", json=body).json())

    def get_driver_pack_catalog(self) -> JsonObject:
        """Fetch enabled driver pack catalog data used for Appium platform selection."""
        return cast("JsonObject", self._send("GET", "/driver-packs/catalog").json())

    def reserve_devices(
        self,
        name: str,
        requirements: JsonObjectList,
        ttl_minutes: int = 60,
        heartbeat_timeout_sec: int = 120,
        created_by: str | None = None,
    ) -> JsonObject:
        """Reserve devices for a test run and return the manager response."""
        resp = self._send(
            "POST",
            "/runs",
            json={
                "name": name,
                "requirements": cast("JsonValue", requirements),
                "ttl_minutes": ttl_minutes,
                "heartbeat_timeout_sec": heartbeat_timeout_sec,
                "created_by": created_by,
            },
            timeout=30,
        )
        return cast("JsonObject", resp.json())

    def signal_ready(self, run_id: str) -> JsonObject:
        return cast("JsonObject", self._send("POST", f"/runs/{run_id}/ready").json())

    def signal_active(self, run_id: str) -> JsonObject:
        return cast("JsonObject", self._send("POST", f"/runs/{run_id}/active").json())

    def heartbeat(self, run_id: str) -> JsonObject:
        return cast("JsonObject", self._send("POST", f"/runs/{run_id}/heartbeat").json())

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
            resp = self._send(
                "POST",
                f"/runs/{run_id}/devices/{device_id}/preparation-failed",
                json={"message": message, "source": source},
            )
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
            resp = self._send("PATCH", f"/sessions/{session_id}/status", json={"status": status}, timeout=5)
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            _raise_or_warn("report session status", suppress_errors, exc)
            return None
        return cast("JsonObject", resp.json())

    def complete_run(self, run_id: str) -> JsonObject:
        return cast("JsonObject", self._send("POST", f"/runs/{run_id}/complete").json())

    def cancel_run(self, run_id: str) -> JsonObject:
        return cast("JsonObject", self._send("POST", f"/runs/{run_id}/cancel").json())

    def cooldown_device(
        self,
        run_id: str,
        device_id: str,
        *,
        reason: str,
        ttl_seconds: int,
    ) -> CooldownResult:
        resp = self._send(
            "POST", f"/runs/{run_id}/devices/{device_id}/cooldown", json={"reason": reason, "ttl_seconds": ttl_seconds}
        )
        return cast("CooldownResult", resp.json())

    def start_heartbeat(self, run_id: str, interval: int = 30) -> HeartbeatThread:
        thread = HeartbeatThread(self.base_url, run_id, interval, auth=self._auth)
        thread.start()
        return thread
