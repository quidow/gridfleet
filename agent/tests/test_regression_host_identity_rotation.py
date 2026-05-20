"""Reproducer for bug 8: ``HostIdentity.set`` rotation produces divergent
host_ids across awaiters. A caller that began ``wait()`` before the first
``set("A")`` reads "A"; a caller that begins ``wait()`` after a subsequent
``set("B")`` reads "B". Long-lived clients that captured "A" then keep using
the stale id forever.

See ``docs/superpowers/specs/2026-05-20-agent-bug-audit.md`` (Bug 8).
"""

from __future__ import annotations

import asyncio

from agent_app.pack.host_identity import HostIdentity


async def test_host_identity_rotation_returns_consistent_value_to_all_waiters() -> None:
    hi = HostIdentity()

    async def early_waiter() -> str:
        return await hi.wait()

    task = asyncio.create_task(early_waiter())
    # Let the early waiter reach its `await self._event.wait()`.
    await asyncio.sleep(0)

    hi.set("host-a")
    # Yield once so the early waiter resumes and reads `_value` *before* the
    # second set() overwrites it.
    await asyncio.sleep(0)

    hi.set("host-b")

    early_value = await task
    late_value = await hi.wait()

    assert early_value == late_value
