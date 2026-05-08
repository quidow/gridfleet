from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTKIT_ROOT = ROOT / "testkit"
DOC_PATHS = [
    TESTKIT_ROOT / "README.md",
    ROOT / "docs" / "reference" / "testkit.md",
]


def test_testkit_docs_do_not_contain_known_stale_tokens() -> None:
    stale_tokens = ["reveal=True", "config_is_masked", "test_ios_screenshot.py"]
    offenders: list[str] = []
    for path in DOC_PATHS:
        text = path.read_text()
        for token in stale_tokens:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)} contains {token}")

    assert offenders == []


def test_examples_are_documented_somewhere() -> None:
    readme = (TESTKIT_ROOT / "README.md").read_text()
    reference = (ROOT / "docs" / "reference" / "testkit.md").read_text()
    combined_docs = f"{readme}\n{reference}"
    example_paths = sorted((TESTKIT_ROOT / "examples").glob("test_*.py"))

    missing: list[str] = []
    for path in example_paths:
        readme_name = f"examples/{path.name}"
        reference_name = f"testkit/examples/{path.name}"
        if readme_name not in combined_docs and reference_name not in combined_docs:
            missing.append(f"no doc mentions {path.name}")

    assert missing == []


def test_documented_example_paths_exist() -> None:
    existing = {f"examples/{path.name}" for path in (TESTKIT_ROOT / "examples").glob("test_*.py")}
    existing |= {f"testkit/examples/{path.name}" for path in (TESTKIT_ROOT / "examples").glob("test_*.py")}
    documented: set[str] = set()
    for path in DOC_PATHS:
        documented.update(re.findall(r"(?:testkit/)?examples/test_[a-z0-9_]+\.py", path.read_text()))

    missing = sorted(example for example in documented if example not in existing)

    assert missing == []
