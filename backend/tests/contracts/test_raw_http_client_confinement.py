"""Raw HTTP clients live in exactly two places.

``tests/contracts/test_repository_transaction_boundaries.py``'s
``test_no_effect_runs_inside_a_transaction_block`` detects effects by call name,
not by construct: a raw ``await client.post(...)`` inside a ``begin()`` block is
invisible to it. That scan is sufficient only while every outbound HTTP request
in ``app/`` is issued through a named operation in ``app/agent_comm/`` or
``app/grid/appium_direct.py``, whose names the scan's sets cover.

This pins that premise. Outside those two locations, ``httpx.AsyncClient`` and
``httpx.Client`` may appear as a *value* -- a factory passed into an agent_comm
operation, or an exception class in an ``except`` clause -- but may never be
instantiated. Constructing one is how a module would start issuing requests the
effect scan cannot see.

Detection covers the conventional ``import httpx2 as httpx`` alias plus a direct
``httpx.AsyncClient(...)`` / ``httpx.Client(...)`` attribute call. A differently
aliased import (``import httpx2 as http_client``), a bare-name import,
subclassing, or any other indirection through which a client gets constructed is
invisible here, exactly like the effect scan this test exists to backstop.

Nothing enforces the ``as httpx`` convention either: ruff's selected set has no
``ICN`` rule, so the alias is a habit, not a constraint. Adding ``ICN`` to close
that would be a repo-wide lint change for a bypass that has never occurred and
that a reviewer reading ``import httpx2 as http_client`` would stop on its own;
the decision is to disclose and leave it. Revisit if a second alias ever appears.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[2] / "app"
CLIENT_NAMES = frozenset({"AsyncClient", "Client"})
# The real package is ``httpx2`` (see pyproject.toml), always imported ``as httpx``; every
# name starting with "httpx2" already starts with "httpx", so a second prefix would be dead weight.
HTTP_MODULE_PREFIX = "httpx"
RAW_CLIENT_OWNERS = ("app/agent_comm/", "app/grid/appium_direct.py")


def _constructs_a_raw_client(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in CLIENT_NAMES:
        return False
    base = node.func.value
    return isinstance(base, ast.Name) and base.id.startswith(HTTP_MODULE_PREFIX)


def test_the_detector_separates_construction_from_reference() -> None:
    """A guard that flagged the factory-value form would be unusable, and one that
    missed the construction form would be vacuous."""
    constructed = ast.parse("async with httpx.AsyncClient() as c:\n    pass")
    referenced = ast.parse("await agent_health(ip, port, http_client_factory=httpx.AsyncClient)")
    assert any(_constructs_a_raw_client(node) for node in ast.walk(constructed))
    assert not any(_constructs_a_raw_client(node) for node in ast.walk(referenced))


def test_raw_http_clients_are_constructed_only_by_the_two_owners() -> None:
    findings: list[str] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        relative = str(path.relative_to(BACKEND_APP.parent))
        if relative.startswith(RAW_CLIENT_OWNERS):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        findings.extend(f"  {relative}:{node.lineno}" for node in ast.walk(tree) if _constructs_a_raw_client(node))
    assert findings == [], (
        "an HTTP client is constructed outside app/agent_comm/ and app/grid/appium_direct.py. "
        "The effect scan in test_repository_transaction_boundaries.py detects effects by call NAME, "
        "so requests issued through a client built here are invisible to it and could sit inside a "
        "begin() block undetected. Route the call through a named agent_comm operation:\n" + "\n".join(findings)
    )
