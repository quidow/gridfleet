import pytest

from app.devices.models import DeviceEventType
from app.devices.models import DeviceOperationalState as S
from app.devices.services.observation_reason import ObservationReason, map_transition_event


def test_verification_passed_not_in_enum() -> None:
    """verification_passed has no DeviceEventType; it must not exist until Task 7 adds one."""
    assert not hasattr(ObservationReason, "verification_passed")


def test_offline_disconnect_maps_to_connectivity_lost() -> None:
    et, sev = map_transition_event(S.available, S.offline, ObservationReason.disconnected)
    assert et is DeviceEventType.connectivity_lost
    assert sev == "warning"


def test_recovery_maps_to_connectivity_restored() -> None:
    et, sev = map_transition_event(S.offline, S.available, ObservationReason.recovered)
    assert et is DeviceEventType.connectivity_restored
    assert sev == "success"


def test_session_start_maps_to_session_started() -> None:
    et, sev = map_transition_event(S.available, S.busy, ObservationReason.session)
    assert et is DeviceEventType.session_started
    assert sev == "info"


@pytest.mark.parametrize(
    ("frm", "to", "reason", "expected_et", "expected_sev"),
    [
        # to=offline transitions
        (S.available, S.offline, ObservationReason.disconnected, DeviceEventType.connectivity_lost, "warning"),
        (S.busy, S.offline, ObservationReason.disconnected, DeviceEventType.connectivity_lost, "warning"),
        (S.available, S.offline, ObservationReason.auto_stopped, DeviceEventType.auto_stopped, "info"),
        (S.available, S.offline, ObservationReason.node_crashed, DeviceEventType.node_crash, "warning"),
        (S.verifying, S.offline, ObservationReason.verification_failed, DeviceEventType.health_check_fail, "warning"),
        # offline→available
        (S.offline, S.available, ObservationReason.recovered, DeviceEventType.connectivity_restored, "success"),
        # →busy
        (S.available, S.busy, ObservationReason.session, DeviceEventType.session_started, "info"),
        # busy→available
        (S.busy, S.available, ObservationReason.session_ended, DeviceEventType.session_ended, "info"),
        # →verifying (no specific event type; falls through to default)
        (
            S.available,
            S.verifying,
            ObservationReason.verification_started,
            DeviceEventType.desired_state_changed,
            "info",
        ),
        # default fallback
        (S.offline, S.verifying, ObservationReason.recovered, DeviceEventType.desired_state_changed, "info"),
    ],
)
def test_map_transition_event_parametrized(
    frm: S,
    to: S,
    reason: ObservationReason,
    expected_et: DeviceEventType,
    expected_sev: str,
) -> None:
    et, sev = map_transition_event(frm, to, reason)
    assert et is expected_et
    assert sev == expected_sev
