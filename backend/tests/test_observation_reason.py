import pytest

from app.devices.models import DeviceEventType
from app.devices.models import DeviceOperationalState as S
from app.devices.services.observation_reason import ObservationReason, map_transition_event


def test_verification_passed_not_in_enum() -> None:
    """verification_passed has no DeviceEventType; it must not exist until Task 7 adds one."""
    assert not hasattr(ObservationReason, "verification_passed")


def test_unmapped_transition_returns_no_event_type() -> None:
    """A transition with no audit-row equivalent maps to (None, _).

    The old EventLogHook wrote NO DeviceEvent row for verification transitions, so the
    reconciler must not invent a spurious ``desired_state_changed`` audit row for them.
    """
    et, _sev = map_transition_event(S.verifying, ObservationReason.verification_started)
    assert et is None


def test_offline_disconnect_maps_to_connectivity_lost() -> None:
    et, sev = map_transition_event(S.offline, ObservationReason.disconnected)
    assert et is DeviceEventType.connectivity_lost
    assert sev == "warning"


def test_recovery_maps_to_connectivity_restored() -> None:
    et, sev = map_transition_event(S.available, ObservationReason.recovered)
    assert et is DeviceEventType.connectivity_restored
    assert sev == "success"


def test_session_start_maps_to_session_started() -> None:
    et, sev = map_transition_event(S.busy, ObservationReason.session)
    assert et is DeviceEventType.session_started
    assert sev == "info"


@pytest.mark.parametrize(
    ("to", "reason", "expected_et", "expected_sev"),
    [
        # to=offline transitions
        (S.offline, ObservationReason.disconnected, DeviceEventType.connectivity_lost, "warning"),
        (S.offline, ObservationReason.auto_stopped, DeviceEventType.auto_stopped, "info"),
        (S.offline, ObservationReason.node_crashed, DeviceEventType.node_crash, "warning"),
        (S.offline, ObservationReason.verification_failed, DeviceEventType.health_check_fail, "warning"),
        # offline→available
        (S.available, ObservationReason.recovered, DeviceEventType.connectivity_restored, "success"),
        # →busy
        (S.busy, ObservationReason.session, DeviceEventType.session_started, "info"),
        # busy→available
        (S.available, ObservationReason.session_ended, DeviceEventType.session_ended, "info"),
        # →verifying (no specific event type; no audit row)
        (S.verifying, ObservationReason.verification_started, None, "info"),
        # default fallback — no audit row
        (S.verifying, ObservationReason.recovered, None, "info"),
    ],
)
def test_map_transition_event_parametrized(
    to: S,
    reason: ObservationReason,
    expected_et: DeviceEventType | None,
    expected_sev: str,
) -> None:
    et, sev = map_transition_event(to, reason)
    assert et is expected_et
    assert sev == expected_sev
