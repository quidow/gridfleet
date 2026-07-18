"""Phase 3 (partial) — the self-contained facts-only pieces:

- ``update_emulator_state`` write-on-diff (M2): no row lock, no write when the
  pushed lifecycle value is unchanged.
- B6 repeat-safe allowlist: the auto-remediation path refuses a non-repeat-safe
  action rather than risk a double-execute on a crash-after-dispatch retry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

from app.core.timeutil import now_utc
from app.devices.services import health as health_module
from app.devices.services import link_repair
from app.devices.services.connectivity import _validated_remediation_action
from app.devices.services.health import DeviceHealthService
from app.packs.manifest import LifecycleAction
from tests.helpers import seed_host_and_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_update_emulator_state_write_on_diff_skips_unchanged(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host, device = await seed_host_and_device(db_session, identity="emu-diff")
    svc = DeviceHealthService(publisher=event_bus)

    lock_calls = 0
    real_lock = health_module._lock

    async def counting_lock(db: AsyncSession, dev: object) -> object:
        nonlocal lock_calls
        lock_calls += 1
        return await real_lock(db, dev)  # type: ignore[arg-type]

    monkeypatch.setattr(health_module, "_lock", counting_lock)
    pushed_at = now_utc()

    # First write (None -> "booted") changes the value: takes the lock, writes.
    await svc.update_emulator_state(db_session, device, "booted", source_time=pushed_at)
    await db_session.commit()
    await db_session.refresh(device)
    assert device.emulator_state == "booted"
    assert lock_calls == 1

    # Re-applying the same value is lock-free and writes nothing.
    await svc.update_emulator_state(db_session, device, "booted", source_time=pushed_at)
    assert lock_calls == 1  # no additional lock taken
    await db_session.refresh(device)
    assert device.emulator_state == "booted"

    # A genuine change takes the lock again.
    await svc.update_emulator_state(db_session, device, "shutdown", source_time=now_utc())
    await db_session.commit()
    await db_session.refresh(device)
    assert device.emulator_state == "shutdown"
    assert lock_calls == 2


def test_repeat_safe_allowlist() -> None:
    assert link_repair.is_repeat_safe_remediation_action("reconnect")
    assert link_repair.is_repeat_safe_remediation_action("release_forwarded_ports")


def test_validated_remediation_action_gate() -> None:
    device = Mock(identity_value="dev-1")
    assert _validated_remediation_action({"recommended_action": "reconnect"}, device) == "reconnect"
    assert (
        _validated_remediation_action({"recommended_action": "release_forwarded_ports"}, device)
        == "release_forwarded_ports"
    )
    assert _validated_remediation_action({"recommended_action": ""}, device) is None
    assert _validated_remediation_action({}, device) is None
    assert LifecycleAction(id="reconnect", remediation=True).remediation is True
    assert LifecycleAction(id="release_forwarded_ports", remediation=True).remediation is True
