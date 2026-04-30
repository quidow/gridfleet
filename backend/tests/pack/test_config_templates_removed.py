from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_backend_has_no_config_template_contracts() -> None:
    offenders: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text()
        if "ConfigTemplate" in text or "config-templates" in text or "apply-template" in text:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []
