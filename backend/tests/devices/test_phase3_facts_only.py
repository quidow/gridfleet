"""Phase 3 (partial) — the self-contained facts-only pieces:

- ``update_emulator_state`` write-on-diff (M2): no row lock, no write when the
  pushed lifecycle value is unchanged.
- B6 repeat-safe allowlist: the auto-remediation path refuses a non-repeat-safe
  action rather than risk a double-execute on a crash-after-dispatch retry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

from app.devices.services import health as health_module
from app.devices.services import link_repair
from app.devices.services.connectivity import _validated_remediation_action
from app.devices.services.health import DeviceHealthService
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

    # First write (None -> "booted") changes the value: takes the lock, writes.
    await svc.update_emulator_state(db_session, device, "booted")
    await db_session.commit()
    await db_session.refresh(device)
    assert device.emulator_state == "booted"
    assert lock_calls == 1

    # Re-applying the same value is lock-free and writes nothing.
    await svc.update_emulator_state(db_session, device, "booted")
    assert lock_calls == 1  # no additional lock taken
    await db_session.refresh(device)
    assert device.emulator_state == "booted"

    # A genuine change takes the lock again.
    await svc.update_emulator_state(db_session, device, "shutdown")
    await db_session.commit()
    await db_session.refresh(device)
    assert device.emulator_state == "shutdown"
    assert lock_calls == 2


def test_repeat_safe_allowlist() -> None:
    assert link_repair.is_repeat_safe_remediation_action("reconnect")
    assert link_repair.is_repeat_safe_remediation_action("release_forwarded_ports")
    assert not link_repair.is_repeat_safe_remediation_action("boot")
    assert not link_repair.is_repeat_safe_remediation_action("shutdown")
    assert not link_repair.is_repeat_safe_remediation_action("state")


def test_validated_remediation_action_gate() -> None:
    device = Mock(identity_value="dev-1")
    assert _validated_remediation_action({"recommended_action": "reconnect"}, device) == "reconnect"
    # Non-repeat-safe action is refused (returns None -> the fold does not dispatch).
    assert _validated_remediation_action({"recommended_action": "boot"}, device) is None
    assert _validated_remediation_action({"recommended_action": ""}, device) is None
    assert _validated_remediation_action({}, device) is None
