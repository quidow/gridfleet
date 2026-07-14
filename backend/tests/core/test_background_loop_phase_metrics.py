from prometheus_client import REGISTRY

from app.core.metrics_recorders import record_background_loop_phase


def _phase_count(loop_name: str, phase: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "background_loop_phase_duration_seconds_count",
            {"loop_name": loop_name, "phase": phase},
        )
        or 0.0
    )


def test_recorder_observes_labeled_phase() -> None:
    before = _phase_count("test_loop", "probe")

    record_background_loop_phase("test_loop", "probe", 1.25)

    assert _phase_count("test_loop", "probe") == before + 1
