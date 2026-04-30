from __future__ import annotations

from pathlib import Path

SCENARIO_DIR = Path(__file__).resolve().parents[2] / "app" / "seeding" / "scenarios"


def test_seed_scenarios_do_not_write_legacy_platform_requirements() -> None:
    offenders: list[str] = []
    for path in sorted(SCENARIO_DIR.glob("*.py")):
        text = path.read_text()
        if '"platform":' in text or "'platform':" in text:
            offenders.append(str(path.relative_to(SCENARIO_DIR.parents[2])))
    assert offenders == []
