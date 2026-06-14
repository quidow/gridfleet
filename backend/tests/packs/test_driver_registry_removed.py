from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_backend_driver_registry_router_is_removed() -> None:
    assert not (ROOT / "app" / "routers" / "drivers.py").exists()
    assert not (ROOT / "app" / "services" / "driver_service.py").exists()
    assert not (ROOT / "app" / "models" / "appium_driver.py").exists()
    assert not (ROOT / "app" / "schemas" / "driver.py").exists()


def test_backend_no_driver_registry_imports_remain() -> None:
    offenders: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text()
        if "AppiumDriver" in text or "driver_service" in text or "routers.drivers" in text:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_legacy_agent_device_routes_removed() -> None:
    agent_main = (ROOT.parent / "agent" / "agent_app" / "main.py").read_text()
    forbidden = [
        '@app.get("/agent/devices")',
        '@app.get("/agent/devices/{connection_target}/properties")',
        '@app.get("/agent/devices/{connection_target}/health")',
        '@app.get("/agent/devices/{connection_target}/telemetry")',
        '@app.post("/agent/devices/{connection_target}/reconnect")',
        '@app.post("/agent/android/network-target/resolve")',
    ]
    for needle in forbidden:
        assert needle not in agent_main, f"Legacy route still present: {needle}"


def test_frontend_no_longer_calls_deleted_driver_registry_api() -> None:
    frontend = ROOT.parent / "frontend" / "src"
    offenders: list[str] = []
    for path in sorted(frontend.rglob("*")):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        text = path.read_text()
        if "/drivers/sync-all" in text or "api/drivers" in text or "`/hosts/${hostId}/drivers" in text:
            offenders.append(str(path.relative_to(ROOT.parent)))
    assert offenders == []
