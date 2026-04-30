from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from adapter.normalize import normalize_device


@dataclass
class _Ctx:
    raw_input: dict[str, Any]
    platform_id: str = "tvos"
    host_id: str = "host-1"


@pytest.mark.asyncio
async def test_normalize_preserves_network_real_device_connection_type() -> None:
    result = await normalize_device(
        _Ctx(
            raw_input={
                "identity_value": "apple-tv-udid",
                "device_type": "real_device",
                "connection_type": "network",
                "os_version": "26.4",
            }
        )
    )

    assert result.connection_type == "network"
    assert result.connection_target == "apple-tv-udid"
    assert result.os_version == "26.4"

