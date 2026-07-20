"""Guard branches in the grid allocation surface: unwired services, claim re-checks,
malformed older tickets, missing node targets, expired/cancelled ticket replays."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import select

if TYPE_CHECKING:
    from collections.abc import Collection

    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.packs.services.capability import StereotypeTemplate

from app.devices.models import Device, DeviceOperationalState
from app.devices.services.intent import IntentService
from app.grid.allocation import AllocationService, _EligibleRow
from app.grid.models import GridQueueStatus, GridSessionQueueTicket
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from tests.helpers import create_device_record, seed_host_and_device, seed_host_and_running_node
from tests.helpers import test_event_bus as event_bus
from tests.packs.factories import seed_test_packs


def _body(**caps: str) -> dict[str, Any]:
    return {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}}


async def _stereotype_stub(
    db: AsyncSession, device: Device, *, template_cache: object | None = None, matching_group_keys: Collection[str] = ()
) -> dict[str, Any]:
    return {"platformName": "Android"}


def _service() -> AllocationService:
    return AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=_stereotype_stub,
    )


@pytest_asyncio.fixture
async def seeded_available_device(db_session: AsyncSession) -> Device:
    await seed_test_packs(db_session)
    _, device, _ = await seed_host_and_running_node(db_session, identity=f"grid-guard-{uuid.uuid4().hex[:8]}")
    return device


@pytest.mark.db
async def test_reap_expired_requires_settings(db_session: AsyncSession) -> None:
    with pytest.raises(RuntimeError, match="settings reader"):
        await _service().reap_expired(db_session)


def _eligible_row(device: Device) -> _EligibleRow:
    """An unreserved eligible-batch row for a device, as ``try_allocate`` builds it."""
    return _EligibleRow(device=device, reservation_run_id=None, static_group_keys=frozenset())


@pytest.mark.db
async def test_claim_rechecks_state_under_lock(db_session: AsyncSession, seeded_available_device: Device) -> None:
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    seeded_available_device.lifecycle_policy_state = {"maintenance_reason": "operator"}
    await db_session.flush()
    result = await _service()._claim(
        db_session,
        ticket=ticket,
        row=_eligible_row(seeded_available_device),
        candidate={},
        run_id=None,
    )
    assert result is None
    assert ticket.status == GridQueueStatus.waiting


@pytest.mark.db
async def test_claim_rechecks_active_sessions_under_lock(
    db_session: AsyncSession, seeded_available_device: Device
) -> None:
    db_session.add(
        Session(
            session_id=f"alloc-{uuid.uuid4()}",
            device_id=seeded_available_device.id,
            status=SessionStatus.pending,
        )
    )
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await _service()._claim(
        db_session,
        ticket=ticket,
        row=_eligible_row(seeded_available_device),
        candidate={},
        run_id=None,
    )
    assert result is None


@pytest.mark.db
async def test_claim_requires_routable_node(db_session: AsyncSession) -> None:
    # device without an AppiumNode -> no target -> claim declines
    await seed_test_packs(db_session)
    _, device = await seed_host_and_device(db_session, identity=f"grid-guard-nonode-{uuid.uuid4().hex[:8]}")
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await _service()._claim(
        db_session,
        ticket=ticket,
        row=_eligible_row(device),
        candidate={},
        run_id=None,
    )
    assert result is None


@pytest.mark.db
async def test_claim_skips_device_with_live_probe_row(
    db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """A running viability probe claims its device with a Session row from birth
    (WS-16.1): _claim's live-session recheck under the row lock sees the row and
    skips the device — the ticket stays waiting and retries on its next poll.
    Replaces the pre-lock probe-lock gate."""
    db_session.add(
        Session(
            session_id=f"probe-{uuid.uuid4()}",
            device_id=seeded_available_device.id,
            test_name=PROBE_TEST_NAME,
            status=SessionStatus.pending,
        )
    )
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await _service()._claim(
        db_session,
        ticket=ticket,
        row=_eligible_row(seeded_available_device),
        candidate={},
        run_id=None,
    )
    assert result is None
    assert ticket.status == GridQueueStatus.waiting


@pytest.mark.db
async def test_claim_proceeds_over_terminal_probe_row(
    db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """A completed probe's terminal row releases the claim: an ended probe row
    must not park the device out of allocation (the birth-row analogue of the
    old stale-lock reclaim)."""
    from datetime import UTC, datetime

    db_session.add(
        Session(
            session_id=f"probe-{uuid.uuid4()}",
            device_id=seeded_available_device.id,
            test_name=PROBE_TEST_NAME,
            status=SessionStatus.passed,
            ended_at=datetime.now(UTC),
        )
    )
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await _service()._claim(
        db_session,
        ticket=ticket,
        row=_eligible_row(seeded_available_device),
        candidate={},
        run_id=None,
    )
    assert result is not None


@pytest.mark.db
async def test_claim_declines_when_node_not_viable_under_lock(
    db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """_claim re-checks node viability under the row lock: a node with an
    unsatisfied restart watermark after _eligible_devices_with_facts ran is declined."""
    from datetime import UTC, datetime, timedelta

    from app.appium_nodes.models import AppiumNode

    node = (
        (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == seeded_available_device.id)))
        .scalars()
        .one()
    )
    node.started_at = datetime.now(UTC) - timedelta(seconds=60)
    node.restart_requested_at = datetime.now(UTC)
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()
    result = await _service()._claim(
        db_session,
        ticket=ticket,
        row=_eligible_row(seeded_available_device),
        candidate={},
        run_id=None,
    )
    assert result is None


@pytest.mark.db
async def test_older_waiter_with_invalid_body_is_skipped(
    db_session: AsyncSession, seeded_available_device: Device
) -> None:
    from datetime import UTC, datetime, timedelta

    older_invalid = GridSessionQueueTicket(
        requested_body={"desiredCapabilities": {}},
        created_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    younger = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add_all([older_invalid, younger])
    await db_session.flush()
    # the malformed older ticket must not block the younger one
    assert await _service().try_allocate(db_session, ticket=younger) is not None


@pytest.mark.db
async def test_mid_restart_device_not_grid_eligible(db_session: AsyncSession, seeded_available_device: Device) -> None:
    """Node-viability predicate (#8): a device whose Appium node has an
    unsatisfied restart watermark must be excluded from grid eligibility,
    matching the run allocator's node filter."""
    from datetime import UTC, datetime, timedelta

    from app.appium_nodes.models import AppiumNode

    node = (
        (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == seeded_available_device.id)))
        .scalars()
        .one()
    )
    # Viable before: the device is eligible.
    eligible_ids = {r.device.id for r in await _service()._eligible_devices_with_facts(db_session, group_keys=())}
    assert seeded_available_device.id in eligible_ids

    node.started_at = datetime.now(UTC) - timedelta(seconds=60)
    node.restart_requested_at = datetime.now(UTC)
    await db_session.flush()

    eligible_ids = {r.device.id for r in await _service()._eligible_devices_with_facts(db_session, group_keys=())}
    assert seeded_available_device.id not in eligible_ids


@pytest.mark.db
async def test_device_match_surface_tolerates_missing_pack(db_session: AsyncSession) -> None:
    # pack tables not seeded -> load_stereotype_template raises LookupError -> the
    # device falls back to grid caps only, but the lookup failure is counted (#1)
    # so an operator can see why the device dropped out of the pool.
    from app.grid.allocation import GRID_STEREOTYPE_LOOKUP_ERROR_TOTAL, device_match_surface

    _, device = await seed_host_and_device(db_session, identity=f"grid-guard-nopack-{uuid.uuid4().hex[:8]}")
    before = GRID_STEREOTYPE_LOOKUP_ERROR_TOTAL._value.get()
    surface = await device_match_surface(db_session, device)
    assert surface.get("gridfleet:deviceId") == str(device.id)
    assert "platformName" not in surface
    assert GRID_STEREOTYPE_LOOKUP_ERROR_TOTAL._value.get() == before + 1


@pytest.mark.db
async def test_device_match_surface_template_cache_collapses_lookups(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #11: two same-pack/platform devices share one template fetch via the
    # per-attempt cache; without it each device would issue its own DB lookup.
    import app.grid.allocation as allocation_module

    await seed_test_packs(db_session)
    _, dev_a, _ = await seed_host_and_running_node(db_session, identity=f"grid-tmpl-a-{uuid.uuid4().hex[:8]}")
    _, dev_b, _ = await seed_host_and_running_node(db_session, identity=f"grid-tmpl-b-{uuid.uuid4().hex[:8]}")
    await db_session.commit()

    calls: list[tuple[str, str]] = []
    real = allocation_module.load_stereotype_template

    async def _counting(db: AsyncSession, *, pack_id: str, platform_id: str) -> StereotypeTemplate:
        calls.append((pack_id, platform_id))
        return await real(db, pack_id=pack_id, platform_id=platform_id)

    monkeypatch.setattr(allocation_module, "load_stereotype_template", _counting)

    cache: dict[tuple[str, str], StereotypeTemplate] = {}
    caps_a = await allocation_module.device_match_surface(db_session, dev_a, template_cache=cache)
    caps_b = await allocation_module.device_match_surface(db_session, dev_b, template_cache=cache)
    assert caps_a["platformName"] == "Android"
    assert caps_b["platformName"] == "Android"
    # Distinct devices -> distinct routing surface, identical pack template.
    assert caps_a["gridfleet:deviceId"] == str(dev_a.id)
    assert caps_b["gridfleet:deviceId"] == str(dev_b.id)
    assert len(calls) == 1


@pytest.mark.db
async def test_device_match_surface_keeps_only_matcher_relevant_base_keys(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Report ⑤ safety net, revised: the pack stereotype base is an open dict[str, Any],
    # so an uploaded pack could carry a constraining key. Identity/group keys AND the
    # pack's appium:platform routing key (the driver-pack platform_id — distinguishes
    # e.g. android_mobile/android_tv/firetv_real, which otherwise share platformName +
    # automationName) MUST survive into the match surface; non-matcher base keys
    # (os_version) and appium:automationName MUST NOT — the matcher ignores them and
    # they only bloat the surface.
    import app.grid.allocation as allocation_module
    from app.packs.services.capability import StereotypeTemplate

    _, device = await seed_host_and_device(db_session, identity=f"grid-uploaded-{uuid.uuid4().hex[:8]}")

    async def _fake_template(db: AsyncSession, *, pack_id: str, platform_id: str) -> StereotypeTemplate:
        return StereotypeTemplate(
            platform_name="Android",
            automation_name="UiAutomator2",
            stereotype_base={
                "appium:platform": "{device.platform_id}",  # constraining + templated -> kept, interpolated
                "appium:os_version": "{device.os_version}",  # non-constraining -> dropped
                "gridfleet:group:ci": True,  # constraining group literal -> kept verbatim
                "appium:udid": "{device.identity_value}",  # constraining + templated -> kept, interpolated
            },
        )

    monkeypatch.setattr(allocation_module, "resolve_pack_for_device", lambda _d: ("p", "plat"))
    monkeypatch.setattr(allocation_module, "load_stereotype_template", _fake_template)

    surface = await allocation_module.device_match_surface(db_session, device)
    assert surface["platformName"] == "Android"
    assert surface["gridfleet:group:ci"] is True
    # A templated identity key must flow through the per-device interpolation path,
    # not merely survive key selection — pins that _interpolate actually substitutes.
    assert surface["appium:udid"] == device.identity_value
    assert surface["gridfleet:deviceId"] == str(device.id)
    assert surface["appium:platform"] == device.platform_id
    assert "appium:os_version" not in surface
    assert "appium:automationName" not in surface


@pytest.mark.db
async def test_try_allocate_does_not_cross_route_platform_ids(db_session: AsyncSession) -> None:
    """Regression: android_mobile/android_tv/firetv_real share platformName=Android +
    appium:automationName=UiAutomator2, but are physically distinct devices. A request
    pinning appium:platform must be satisfied only by a device of that platform_id —
    previously the allocator ignored appium:platform entirely and could hand back
    whichever same-platformName device it reached first (regression: a firetv_real
    request was silently allocated to an android_mobile device)."""
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode
    from app.grid.allocation import device_match_surface

    await seed_test_packs(db_session)
    host, mobile_device, _ = await seed_host_and_running_node(
        db_session, identity=f"grid-mobile-{uuid.uuid4().hex[:8]}"
    )
    tv_device = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value=f"grid-tv-{uuid.uuid4().hex[:8]}",
        name="Chromecast HD",
        platform_id="android_tv",
        operational_state=DeviceOperationalState.available,
    )
    tv_node = AppiumNode(
        device_id=tv_device.id,
        port=4731,
        pid=12346,
        active_connection_target=tv_device.connection_target,
        desired_state=AppiumDesiredState.running,
        desired_port=4731,
    )
    db_session.add(tv_node)
    await db_session.commit()

    service = AllocationService(
        intent_factory=IntentService,
        publisher=event_bus,
        stereotype_provider=device_match_surface,
    )
    ticket = GridSessionQueueTicket(
        requested_body=_body(
            **{
                "platformName": "Android",
                "appium:automationName": "UiAutomator2",
                "appium:platform": "android_tv",
            }
        )
    )
    db_session.add(ticket)
    await db_session.flush()

    result = await service.try_allocate(db_session, ticket=ticket)

    assert result is not None
    assert result.device_id == tv_device.id
    assert result.device_id != mobile_device.id


@pytest.mark.db
async def test_allocate_replay_of_cancelled_ticket_is_400(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="iOS"), status=GridQueueStatus.cancelled)
    db_session.add(ticket)
    await db_session.commit()
    resp = await client.post(
        "/internal/grid/create-session", json={"body": _body(platformName="iOS"), "ticket": str(ticket.id)}
    )
    assert resp.status_code == 400
    assert resp.json()["status"] == "invalid"


@pytest.mark.db
async def test_allocate_replay_of_expired_ticket_is_410(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="iOS"), status=GridQueueStatus.expired)
    db_session.add(ticket)
    await db_session.commit()
    resp = await client.post(
        "/internal/grid/create-session", json={"body": _body(platformName="iOS"), "ticket": str(ticket.id)}
    )
    assert resp.status_code == 410
    assert resp.json()["status"] == "expired"


@pytest.mark.db
async def test_routes_skips_sessions_without_routable_device(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    # running session with no device at all and no stored target
    db_session.add(Session(session_id="no-device-1", device_id=None, status=SessionStatus.running))
    # running session on a device without an AppiumNode (no live target, no stored target)
    _, device = await seed_host_and_device(db_session, identity=f"grid-guard-route-{uuid.uuid4().hex[:8]}")
    db_session.add(Session(session_id="no-node-1", device_id=device.id, status=SessionStatus.running))
    await db_session.commit()
    resp = await client.get("/internal/grid/routes")
    assert resp.status_code == 200
    listed = {r["session_id"] for r in resp.json()["routes"]}
    assert "no-device-1" not in listed
    assert "no-node-1" not in listed


@pytest.mark.db
async def test_routes_uses_live_target_when_available(
    client: AsyncClient, db_session: AsyncSession, seeded_available_device: Device
) -> None:
    # Live node target present: it wins even when a (stale) stored target also exists.
    db_session.add(
        Session(
            session_id="live-1",
            device_id=seeded_available_device.id,
            status=SessionStatus.running,
            router_target="http://stale:9999",
        )
    )
    await db_session.commit()
    resp = await client.get("/internal/grid/routes")
    assert resp.status_code == 200
    routes = {r["session_id"]: r["target"] for r in resp.json()["routes"]}
    assert "live-1" in routes
    assert routes["live-1"] != "http://stale:9999"  # live node_target, not the stored fallback


@pytest.mark.db
async def test_routes_falls_back_to_stored_target_when_node_target_gone(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """#6: a running session whose device lost its node target (recovery backoff
    detached the AppiumNode) still appears in /routes with the stored router_target."""
    from app.appium_nodes.models import AppiumNode

    await seed_test_packs(db_session)
    _, device, node = await seed_host_and_running_node(db_session, identity=f"grid-guard-stale-{uuid.uuid4().hex[:8]}")
    db_session.add(
        Session(
            session_id="stored-1",
            device_id=device.id,
            status=SessionStatus.running,
            router_target="http://stored.example:4730",
        )
    )
    await db_session.commit()
    # Recovery backoff detached the node -> live node_target() is now None.
    await db_session.delete(await db_session.get(AppiumNode, node.id))
    await db_session.commit()

    resp = await client.get("/internal/grid/routes")
    assert resp.status_code == 200
    routes = {r["session_id"]: r["target"] for r in resp.json()["routes"]}
    assert routes.get("stored-1") == "http://stored.example:4730"


@pytest.mark.db
async def test_not_accepting_device_not_grid_eligible(
    db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """Soft-gate (P1): a healthy, available device whose node has
    accepting_new_sessions=False must be excluded from grid eligibility — the
    warm-park lever that cooldown (Stage 2 P2) rides on."""
    from app.appium_nodes.models import AppiumNode

    node = (
        (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == seeded_available_device.id)))
        .scalars()
        .one()
    )
    # Accepting before: the device is eligible.
    eligible_ids = {r.device.id for r in await _service()._eligible_devices_with_facts(db_session, group_keys=())}
    assert seeded_available_device.id in eligible_ids

    node.accepting_new_sessions = False  # not a guard-protected column
    await db_session.flush()

    eligible_ids = {r.device.id for r in await _service()._eligible_devices_with_facts(db_session, group_keys=())}
    assert seeded_available_device.id not in eligible_ids


@pytest.mark.db
async def test_try_allocate_skips_excluded_device(db_session: AsyncSession, seeded_driver_packs: None) -> None:
    _ = seeded_driver_packs
    _, dev_a, _ = await seed_host_and_running_node(db_session, identity=f"grid-excl-a-{uuid.uuid4().hex[:8]}")
    _, dev_b, _ = await seed_host_and_running_node(db_session, identity=f"grid-excl-b-{uuid.uuid4().hex[:8]}")
    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()

    eligible_ids = {
        r.device.id
        for r in await _service()._eligible_devices_with_facts(db_session, group_keys=(), exclude_device_ids={dev_a.id})
    }
    assert dev_a.id not in eligible_ids
    assert dev_b.id in eligible_ids

    result = await _service().try_allocate(db_session, ticket=ticket, exclude_device_ids={dev_a.id})

    assert result is not None
    assert result.device_id == dev_b.id


@pytest.mark.db
async def test_claim_declines_when_node_not_accepting_under_lock(
    db_session: AsyncSession, seeded_available_device: Device
) -> None:
    """The lock-time recheck must also honor the soft-gate: if a device was
    eligible at _eligible_devices_with_facts time but its node flipped to
    accepting_new_sessions=False before the row lock, _claim declines and the
    ticket stays waiting."""
    from app.appium_nodes.models import AppiumNode

    ticket = GridSessionQueueTicket(requested_body=_body(platformName="Android"))
    db_session.add(ticket)
    await db_session.flush()

    node = (
        (await db_session.execute(select(AppiumNode).where(AppiumNode.device_id == seeded_available_device.id)))
        .scalars()
        .one()
    )
    node.accepting_new_sessions = False
    await db_session.flush()

    result = await _service()._claim(
        db_session,
        ticket=ticket,
        row=_eligible_row(seeded_available_device),
        candidate={},
        run_id=None,
    )
    assert result is None
    assert ticket.status == GridQueueStatus.waiting
