from app.devices.services.connectivity import _summarize_unhealthy_result


def test_summarize_unhealthy_result_covers_detail_and_failed_checks() -> None:
    assert _summarize_unhealthy_result(None) == "Device health checks failed"
    assert _summarize_unhealthy_result({"detail": "ADB not responsive"}) == "ADB not responsive"
    assert (
        _summarize_unhealthy_result(
            {"checks": [{"check_id": "adb_connected", "ok": False}, {"check_id": "ip_ping", "ok": True}]}
        )
        == "Failed checks: adb connected"
    )
    assert _summarize_unhealthy_result({"healthy": True, "checks": []}) == "Device health checks failed"
