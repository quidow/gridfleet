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
