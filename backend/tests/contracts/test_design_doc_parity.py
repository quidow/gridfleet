"""Design-doc<->code parity for the two enumerations the design docs keep.

Docs 1-5 under docs/design/ prune mechanical enumerations to pointers
(WS-17.1); the two tables that stay -- doc 3's scheduler loop roster and
doc 4's backend->agent dial catalog -- recurred stale after every manual
doc pass, so they are pinned here in both directions, same shape as
``test_events_doc_parity``.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from app import main as app_main

REPO_ROOT = Path(__file__).resolve().parents[3]
LOOPS_DOC = REPO_ROOT / "docs" / "design" / "03-health-and-reconciliation.md"

_LOOP_SECTION = "## Loop registry"
_LOOP_ROW = re.compile(r"^\|\s*`([a-z_]+_loop)`\s*\|")
_LOOP_NAME_IN_SOURCE = re.compile(r'"([a-z_]+_loop)"')


def _section(doc: Path, heading: str) -> str:
    text = doc.read_text(encoding="utf-8")
    assert heading in text, f"{heading!r} heading missing from {doc}"
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def _documented_loop_names() -> set[str]:
    names = {m.group(1) for line in _section(LOOPS_DOC, _LOOP_SECTION).splitlines() if (m := _LOOP_ROW.match(line))}
    assert names, "parsed zero loop rows from doc 3 -- table format changed?"
    return names


def _roster_loop_names() -> set[str]:
    source = inspect.getsource(app_main._build_leader_loop_tasks)
    names = set(_LOOP_NAME_IN_SOURCE.findall(source))
    assert names, "parsed zero loop names from _build_leader_loop_tasks -- roster format changed?"
    return names


def test_every_roster_loop_has_a_doc_row() -> None:
    missing = _roster_loop_names() - _documented_loop_names()
    assert not missing, f"scheduler loops missing from doc 3's roster table: {sorted(missing)}"


def test_every_documented_loop_exists_in_roster() -> None:
    stale = _documented_loop_names() - _roster_loop_names()
    assert not stale, f"doc 3 documents loops absent from _build_leader_loop_tasks: {sorted(stale)}"
