"""Public GridFleet client helpers for external test suites."""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import signal
import threading
import time
from typing import Any, cast

import httpx

GRID_URL = os.getenv("GRID_URL", "http://localhost:4444")
GRIDFLEET_API_URL = os.getenv("GRIDFLEET_API_URL", "http://localhost:8000/api")
GRIDFLEET_TESTKIT_USERNAME = os.getenv("GRIDFLEET_TESTKIT_USERNAME")
GRIDFLEET_TESTKIT_PASSWORD = os.getenv("GRIDFLEET_TESTKIT_PASSWORD")

logger = logging.getLogger("gridfleet_testkit")


def _default_auth() -> httpx.BasicAuth | None:
    """Build httpx Basic auth from env vars, or return None when unset."""
    username = GRIDFLEET_TESTKIT_USERNAME
    password = GRIDFLEET_TESTKIT_PASSWORD
    if not username or not password:
        return None
    return httpx.BasicAuth(username, password)


class NoClaimableDevicesError(RuntimeError):
    """Raised when the manager reports that no run devices are claimable yet."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_sec: int,
        next_available_at: str | None = None,
    ) -> None:
        self.retry_after_sec = retry_after_sec
        self.next_available_at = next_available_at
        super().__init__(message)


def _raise_for_status(resp: Any) -> None:
    if resp.status_code == 409:
        try:
            payload = resp.json()
        except Exception:
            payload = None
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            details = error.get("details")
            if isinstance(details, dict) and details.get("error") == "no_claimable_devices":
                retry_after = details.get("retry_after_sec")
                if not isinstance(retry_after, int):
                    retry_after = 5
                next_available_at = details.get("next_available_at")
                raise NoClaimableDevicesError(
                    str(error.get("message") or "No unclaimed devices available in this run"),
                    retry_after_sec=retry_after,
                    next_available_at=next_available_at if isinstance(next_available_at, str) else None,
                )
    resp.raise_for_status()


class HeartbeatThread(threading.Thread):
    """Background thread that sends periodic heartbeat pings for an active test run."""

    def __init__(
        self,
        base_url: str,
        run_id: str,
        interval: int = 30,
        auth: httpx.BasicAuth | None = None,
    ):
        super().__init__(daemon=True)
        self.base_url = base_url.rstrip("/")
        self.run_id = run_id
        self.interval = interval
        self._auth = auth
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                resp = httpx.post(
                    f"{self.base_url}/runs/{self.run_id}/heartbeat",
                    timeout=10,
                    auth=self._auth,
                )
                resp.raise_for_status()
                result = resp.json()
                if result.get("state") in ("expired", "cancelled"):
                    logger.warning("Run %s is %s, stopping heartbeat", self.run_id, result["state"])
                    break
            except Exception:
                logger.debug("Heartbeat failed for run %s, will retry", self.run_id)

    def stop(self) -> None:
        self._stop_event.set()


class GridFleetClient:
    """Client for the GridFleet API, used by test fixtures and CI flows."""

    def __init__(
        self,
        base_url: str = GRIDFLEET_API_URL,
        auth: httpx.BasicAuth | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._auth = auth if auth is not None else _default_auth()

    def get_device_config(self, connection_target: str, reveal: bool = True) -> dict[str, Any]:
        """Fetch device config by looking up the current runtime connection target."""
        resp = httpx.get(
            f"{self.base_url}/devices",
            params={"connection_target": connection_target},
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        devices = cast("list[dict[str, Any]]", resp.json())
        if not devices:
            raise ValueError(f"No device found with connection target: {connection_target}")
        device_id = devices[0]["id"]
        config_resp = httpx.get(
            f"{self.base_url}/devices/{device_id}/config",
            params={"reveal": str(reveal).lower()},
            timeout=10,
            auth=self._auth,
        )
        config_resp.raise_for_status()
        return cast("dict[str, Any]", config_resp.json())

    def get_device_capabilities(self, device_id: str) -> dict[str, Any]:
        """Fetch the current Appium capabilities for a specific device."""
        resp = httpx.get(
            f"{self.base_url}/devices/{device_id}/capabilities",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def get_driver_pack_catalog(self) -> dict[str, Any]:
        """Fetch enabled driver pack catalog data used for Appium platform selection."""
        resp = httpx.get(
            f"{self.base_url}/driver-packs/catalog",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def reserve_devices(
        self,
        name: str,
        requirements: list[dict[str, Any]],
        ttl_minutes: int = 60,
        heartbeat_timeout_sec: int = 120,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Reserve devices for a test run and return the manager response."""
        resp = httpx.post(
            f"{self.base_url}/runs",
            json={
                "name": name,
                "requirements": requirements,
                "ttl_minutes": ttl_minutes,
                "heartbeat_timeout_sec": heartbeat_timeout_sec,
                "created_by": created_by,
            },
            timeout=30,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def signal_ready(self, run_id: str) -> None:
        httpx.post(
            f"{self.base_url}/runs/{run_id}/ready",
            timeout=10,
            auth=self._auth,
        ).raise_for_status()

    def signal_active(self, run_id: str) -> None:
        httpx.post(
            f"{self.base_url}/runs/{run_id}/active",
            timeout=10,
            auth=self._auth,
        ).raise_for_status()

    def heartbeat(self, run_id: str) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/heartbeat",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def claim_device(self, run_id: str, *, worker_id: str) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/claim",
            json={"worker_id": worker_id},
            timeout=10,
            auth=self._auth,
        )
        _raise_for_status(resp)
        return cast("dict[str, Any]", resp.json())

    def release_device(self, run_id: str, *, device_id: str, worker_id: str) -> None:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/release",
            json={"device_id": device_id, "worker_id": worker_id},
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()

    def release_device_with_cooldown(
        self,
        run_id: str,
        *,
        device_id: str,
        worker_id: str,
        reason: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/devices/{device_id}/release-with-cooldown",
            json={"worker_id": worker_id, "reason": reason, "ttl_seconds": ttl_seconds},
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def claim_device_with_retry(
        self,
        run_id: str,
        *,
        worker_id: str,
        max_wait_sec: int = 300,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max_wait_sec
        while True:
            try:
                return self.claim_device(run_id, worker_id=worker_id)
            except NoClaimableDevicesError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(exc.retry_after_sec, max(1, int(remaining))))

    def report_preparation_failure(
        self,
        run_id: str,
        device_id: str,
        message: str,
        source: str = "ci_preparation",
    ) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/devices/{device_id}/preparation-failed",
            json={"message": message, "source": source},
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def complete_run(self, run_id: str) -> None:
        httpx.post(
            f"{self.base_url}/runs/{run_id}/complete",
            timeout=10,
            auth=self._auth,
        ).raise_for_status()

    def cancel_run(self, run_id: str) -> None:
        httpx.post(
            f"{self.base_url}/runs/{run_id}/cancel",
            timeout=10,
            auth=self._auth,
        ).raise_for_status()

    def start_heartbeat(self, run_id: str, interval: int = 30) -> HeartbeatThread:
        thread = HeartbeatThread(self.base_url, run_id, interval, auth=self._auth)
        thread.start()
        return thread


def register_run_cleanup(
    client: GridFleetClient,
    run_id: str,
    heartbeat_thread: HeartbeatThread | None = None,
) -> None:
    """Register exit and signal handlers that release reserved devices."""

    def cleanup(*_args: object) -> None:
        if heartbeat_thread:
            heartbeat_thread.stop()
        try:
            client.complete_run(run_id)
        except Exception:
            with contextlib.suppress(Exception):
                client.cancel_run(run_id)

    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
