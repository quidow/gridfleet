"""Transaction-boundary contract for the portability import command.

``app/portability/services/import_bundle.py`` owns its own sessions via an
injected ``session_factory``: a short read for validation, one transaction for
group definitions, one transaction per bounded device batch (each row wrapped
in its own savepoint), and one transaction per bounded membership batch. This
module pins two things a plain outcome assertion cannot: the AST shape (zero
direct commit/rollback, exactly one ``begin_nested()`` owner) and the
constraint-race session-lifecycle rule (a failed transaction's session is
closed before a fresh session reads the collision).
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from app.devices.models import DeviceGroup, GroupType
from app.portability.schemas import ExportBundle, ExportedDeviceGroup, ImportCommitRequest
from app.portability.services import import_bundle as import_bundle_module
from app.portability.services.hash import compute_bundle_hash
from app.portability.services.import_bundle import GroupKeyCollisionError, PortabilityImportService
from app.verification.services.service import VerificationService
from tests.fakes.session_factory import RecordingSessionFactory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BACKEND_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = BACKEND_ROOT / "app" / "portability" / "services" / "import_bundle.py"


def _calls_named(tree: ast.AST, names: set[str]) -> list[tuple[int, str, str]]:
    """Every call to an attribute named in *names*, tagged with its owning function.

    The owner is the innermost enclosing ``def``/``async def``, or ``<module>``
    for a call at module scope. Mirrors
    ``tests/contracts/test_repository_transaction_boundaries.py``'s scoping helper.
    """
    functions = [
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    results: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in names:
            enclosing = sorted((f for f in functions if f[0] <= node.lineno <= f[1]), key=lambda f: f[0])
            owner = enclosing[-1][2] if enclosing else "<module>"
            results.append((node.lineno, node.func.attr, owner))
    return results


def test_import_bundle_module_has_no_direct_commit_or_rollback() -> None:
    """Every transaction boundary is a native ``async with`` context; none is a bare call."""
    tree = ast.parse(MODULE_PATH.read_text(), filename=str(MODULE_PATH))
    calls = _calls_named(tree, {"commit", "rollback"})
    assert calls == [], f"import_bundle.py must own no explicit commit/rollback: {calls}"


def test_import_bundle_module_has_exactly_one_begin_nested_owner() -> None:
    """This module's ``begin_nested()`` has exactly one owner: the public per-row savepoint.

    Scoped to ``import_bundle.py`` only. Other modules own their own
    ``begin_nested()`` calls for their own reasons (e.g.
    ``app/devices/services/groups.py``, ``app/devices/services/intent_reconciler.py``)
    and are out of scope for this assertion.
    """
    tree = ast.parse(MODULE_PATH.read_text(), filename=str(MODULE_PATH))
    calls = _calls_named(tree, {"begin_nested"})
    owners = {owner for _, _, owner in calls}
    assert len(calls) == 1, f"expected exactly one begin_nested() call site: {calls}"
    assert owners == {"_insert_row_with_savepoint"}, (
        f"begin_nested() must be owned only by _insert_row_with_savepoint: {calls}"
    )


@pytest.mark.asyncio
@pytest.mark.db
async def test_group_key_collision_reads_through_a_fresh_session_after_the_failed_one_closes(
    db_session_maker: async_sessionmaker[AsyncSession],
    seeded_driver_packs: None,
) -> None:
    """A concurrent creator wins the unique-key race; the loser translates the error via a new session.

    Nothing reserves a bundle's group keys during ``validate_bundle``'s
    pre-check, so a key created by a peer between validation and the commit's
    definition-transaction flush is invisible to the pre-check and visible only
    when the flush hits ``ix_device_groups_key`` for real. Patch the pre-check
    to see none of that -- exactly what the losing caller sees -- while a real
    colliding row already sits in the table, so the flush's IntegrityError is a
    genuine constraint violation, not a simulated one.
    """
    _ = seeded_driver_packs
    suffix = uuid.uuid4().hex[:8]
    collided_key = f"lab-fleet-{suffix}"
    async with db_session_maker() as seed_db:
        seed_db.add(DeviceGroup(key=collided_key, name=collided_key, group_type=GroupType.static))
        await seed_db.commit()

    bundle = ExportBundle(
        schema_version=2,
        exported_at=datetime.now(UTC),
        source_instance="alpha",
        groups=[ExportedDeviceGroup(key=collided_key, name=collided_key, group_type=GroupType.static)],
        devices=[],
    )
    request = ImportCommitRequest(bundle=bundle, bundle_hash=compute_bundle_hash(bundle), mappings=[])

    real_load = import_bundle_module._load_existing_group_keys
    calls = 0

    async def _sees_no_existing_keys(session: AsyncSession, keys: set[str]) -> set[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return set()
        return await real_load(session, keys)

    factory = RecordingSessionFactory(db_session_maker)
    service = PortabilityImportService(verification_enqueuer=VerificationService(), session_factory=factory)

    with (
        patch("app.portability.services.import_bundle._load_existing_group_keys", _sees_no_existing_keys),
        pytest.raises(GroupKeyCollisionError) as exc_info,
    ):
        await service.commit_import(request)

    assert exc_info.value.keys == [collided_key], "the 409 must name only the key that actually collided"

    # session 0 = validate's read; session 1 = the definition transaction that
    # lost the race and rolled back; session 2 = the fresh conflict-read session
    # opened only after that rollback.
    assert len(factory.sessions) == 3, [id(s) for s in factory.sessions]
    assert factory.sessions[1] is not factory.sessions[2], (
        "the collision read must open a new session rather than reuse the failed one"
    )
    assert not factory.sessions[1].in_transaction(), "the failed definition session must be closed before the reread"
