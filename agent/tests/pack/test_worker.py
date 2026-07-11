from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _write_adapter(site: Path, source: str) -> None:
    package = site / "adapter"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(source)


async def _start_worker(site: Path, *, timeout: float = 30.0) -> asyncio.subprocess.Process:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent_app.pack.worker",
        "--pack-id",
        "test-pack",
        "--release",
        "1.0.0",
        "--site",
        str(site),
        "--hook-timeout",
        str(timeout),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    handshake = json.loads((await proc.stdout.readline()).decode())
    proc._test_handshake = handshake  # type: ignore[attr-defined]
    return proc


async def _request(
    proc: asyncio.subprocess.Process, req_id: int, hook: str, payload: dict[str, object]
) -> dict[str, object]:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(
        (json.dumps({"id": req_id, "hook": hook, "payload": payload}, separators=(",", ":")) + "\n").encode()
    )
    await proc.stdin.drain()
    return json.loads((await proc.stdout.readline()).decode())


async def _stop_worker(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        if proc.stdin is not None:
            proc.stdin.close()
        await proc.wait()


@pytest.mark.asyncio
async def test_handshake_reports_hooks_and_contributions(tmp_path: Path) -> None:
    _write_adapter(
        tmp_path,
        """
from agent_app.pack.adapter_types import SubprocessEnvContribution

class Adapter:
    async def health_check(self, ctx):
        return []
    async def discover(self, ctx):
        return []
    async def normalize_device(self, ctx):
        raise RuntimeError("boom")
    def subprocess_env(self):
        return SubprocessEnvContribution(env_vars={"X_TEST": "1"})
    def tool_versions(self):
        return {"testtool": "1.2.3"}
""",
    )
    proc = await _start_worker(tmp_path)
    try:
        handshake = proc._test_handshake  # type: ignore[attr-defined]
        assert handshake["supported_hooks"] == ["discover", "health_check", "normalize_device"]
        assert handshake["subprocess_env"] == {"env_vars": {"X_TEST": "1"}, "extra_path_dirs": []}
        assert handshake["tool_versions"] == {"testtool": "1.2.3"}
    finally:
        await _stop_worker(proc)


@pytest.mark.asyncio
async def test_hook_roundtrip_and_error_shapes(tmp_path: Path) -> None:
    _write_adapter(
        tmp_path,
        """
class Adapter:
    async def discover(self, ctx):
        return []
    async def normalize_device(self, ctx):
        raise RuntimeError("boom")
""",
    )
    proc = await _start_worker(tmp_path)
    try:
        discover = await _request(proc, 7, "discover", {"ctx": {"host_id": "h", "platform_id": "p"}})
        assert discover["id"] == 7
        assert discover["ok"] is True
        assert discover["result"] == []
        failed = await _request(
            proc, 8, "normalize_device", {"ctx": {"host_id": "h", "platform_id": "p", "raw_input": {}}}
        )
        assert failed["id"] == 8
        assert failed["ok"] is False
        assert failed["error"]["kind"] == "exception"  # type: ignore[index]
        assert "boom" in failed["error"]["message"]  # type: ignore[index]
        unknown = await _request(proc, 9, "telemetry", {"ctx": {}})
        assert unknown["id"] == 9
        assert unknown["error"]["kind"] == "unknown_hook"  # type: ignore[index]
    finally:
        await _stop_worker(proc)


@pytest.mark.asyncio
async def test_worker_timeout_is_soft_reported(tmp_path: Path) -> None:
    _write_adapter(
        tmp_path,
        """
import asyncio

class Adapter:
    async def health_check(self, ctx):
        await asyncio.sleep(10)
        return []
""",
    )
    proc = await _start_worker(tmp_path, timeout=0.2)
    try:
        response = await asyncio.wait_for(
            _request(proc, 1, "health_check", {"ctx": {"device_identity_value": "d", "allow_boot": False}}),
            timeout=2,
        )
        assert response["error"]["kind"] == "timeout"  # type: ignore[index]
        assert proc.returncode is None
    finally:
        await _stop_worker(proc)


@pytest.mark.asyncio
async def test_adapter_prints_do_not_corrupt_protocol(tmp_path: Path) -> None:
    _write_adapter(
        tmp_path,
        """
class Adapter:
    async def discover(self, ctx):
        print("garbage on stdout")
        return []
""",
    )
    proc = await _start_worker(tmp_path)
    try:
        response = await _request(proc, 1, "discover", {"ctx": {"host_id": "h", "platform_id": "p"}})
        assert response["id"] == 1
        assert response["ok"] is True
        assert response["result"] == []
    finally:
        await _stop_worker(proc)
