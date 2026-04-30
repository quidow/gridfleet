from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "gridfleet_testkit"

FORBIDDEN_PATTERNS = (
    "GridFleetPlatform",
    "PLATFORM_ANDROID_MOBILE",
    "PLATFORM_ANDROID_TV",
    "PLATFORM_FIRETV",
    "PLATFORM_IOS",
    "PLATFORM_TVOS",
    "PLATFORM_ROKU",
    "GRIDFLEET_PLATFORM_TO_APPIUM_PLATFORM",
    "GRIDFLEET_PLATFORM_ROUTING_HINTS",
)


def test_testkit_package_has_no_legacy_platform_aliases() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        text = path.read_text()
        for pattern in FORBIDDEN_PATTERNS:
            if pattern in text:
                offenders.append(f"{path.relative_to(ROOT)} contains {pattern}")

    assert offenders == []
