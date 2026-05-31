from app.devices.services.state_derivation import SHADOW_STATE_MISMATCH


def test_shadow_metric_exists() -> None:
    # Counter with labels for which axis diverged.
    SHADOW_STATE_MISMATCH.labels(axis="operational").inc(0)
    SHADOW_STATE_MISMATCH.labels(axis="hold").inc(0)
