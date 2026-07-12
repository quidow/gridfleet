"""Catalog<->doc parity for the public event contract.

An event shipped in ``app.events.catalog`` without a row in
docs/reference/events.md — drift that recurred after a manual doc pass.
This test mechanizes the parity in both directions: every catalog event
name must have a row in the doc's "Emitted Event Names" tables, and every
documented name must still exist in the catalog.

The removed-events note at the top of the doc (e.g.
``device.availability_changed``) lives in a blockquote outside the parsed
section, so it is exempt by construction. Field-detail tables inside the
section (e.g. the ``device.crashed`` payload table) are excluded because
their first-column names contain no dot.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.events.catalog import PUBLIC_EVENT_NAME_SET

REPO_ROOT = Path(__file__).resolve().parents[3]
EVENTS_DOC = REPO_ROOT / "docs" / "reference" / "events.md"

_SECTION_HEADING = "## Emitted Event Names"
_EVENT_ROW = re.compile(r"^\|\s*`([a-z_]+(?:\.[a-z_]+)+)`\s*\|")


def _documented_event_names() -> set[str]:
    text = EVENTS_DOC.read_text(encoding="utf-8")
    assert _SECTION_HEADING in text, f"{_SECTION_HEADING!r} heading missing from {EVENTS_DOC}"
    section = text.split(_SECTION_HEADING, 1)[1].split("\n## ", 1)[0]
    names = {match.group(1) for line in section.splitlines() if (match := _EVENT_ROW.match(line))}
    assert names, "parsed zero event rows from events.md — table format changed?"
    return names


def test_every_catalog_event_has_a_doc_row() -> None:
    missing = PUBLIC_EVENT_NAME_SET - _documented_event_names()
    assert not missing, f"catalog events missing from docs/reference/events.md: {sorted(missing)}"


def test_every_documented_event_exists_in_catalog() -> None:
    stale = _documented_event_names() - PUBLIC_EVENT_NAME_SET
    assert not stale, f"events.md documents event names absent from the catalog: {sorted(stale)}"
