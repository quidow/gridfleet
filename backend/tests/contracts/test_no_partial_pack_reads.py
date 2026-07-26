"""Static contract for partial reads of the driver-pack models.

``load_pack_catalog`` (``app/packs/services/catalog_view.py``) names its
columns with ``load_only``. Every column it does not name is *deferred* on the
``DriverPack`` / ``DriverPackRelease`` / ``DriverPackPlatform`` rows it loads,
and under ``AsyncSession`` a deferred read raises ``MissingGreenlet`` instead
of lazy-loading. That is safe only because those rows enter the loading
session's identity map and every other pack read in ``app/`` selects the full
entity, which repopulates them.

A second partial reader breaks that silently. It does not fail where it is
written: it fails when some *other* code path, on a session that happens to
have already read the catalog, touches a column neither reader named. The
stack trace points at the innocent consumer. This scan is the only thing
standing between that bug and production.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[2] / "app"

# The three models ``load_pack_catalog`` reads partially. An option naming one
# of these leaves every unnamed column deferred on rows that outlive the read
# in the session identity map.
PACK_MODELS = frozenset({"DriverPack", "DriverPackRelease", "DriverPackPlatform"})

# The two loader options that produce deferred columns. ``raiseload`` is
# deliberately absent: it fails immediately and locally at the offending
# access, which is the opposite of the failure profile this guard exists for.
DEFERRING_OPTIONS = frozenset({"load_only", "defer"})

# One entry per file allowed to read the pack models partially, with the reason
# it is safe. This table is the documentation: a second partial reader is a
# legitimate thing to want, and adding one means extending this table *and*
# showing that the identity-map interaction still holds for the columns the two
# readers disagree about.
PARTIAL_PACK_READERS: dict[str, str] = {
    "app/packs/services/catalog_view.py": (
        "load_pack_catalog names exactly the columns project_pack reads, projects "
        "them into values immediately, and returns no ORM row; every other pack "
        "read in app/ selects the full entity and repopulates the deferred columns."
    ),
}


def _option_calls(tree: ast.Module) -> list[ast.Call]:
    """Every ``load_only(...)`` / ``defer(...)`` call in *tree*, bare or chained.

    Chained options (``joinedload(...).load_only(...)``) are an ``Attribute``
    call; bare ones are a ``Name`` call. Both count.
    """
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_attribute_option = isinstance(func, ast.Attribute) and func.attr in DEFERRING_OPTIONS
        is_name_option = isinstance(func, ast.Name) and func.id in DEFERRING_OPTIONS
        if is_attribute_option or is_name_option:
            calls.append(node)
    return calls


def _named_models(call: ast.Call) -> tuple[frozenset[str], bool]:
    """The model classes *call*'s positional arguments name, and whether any
    argument failed to name one.

    ``load_only(DriverPack.state)`` names its model as the ``X`` of an
    ``X.column`` attribute access. Anything else — a bare name, a star-arg, a
    string column name — cannot be read from the source, and the scan reports
    it rather than passing over it. Two forms it does not catch at all: an
    ``aliased()`` handle, whose variable name the scan mistakes for the model,
    and an aliased import of the option itself (``from sqlalchemy.orm import
    load_only as lo``), which the call-name match never sees.
    """
    models: set[str] = set()
    unreadable = False
    for arg in call.args:
        if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
            models.add(arg.value.id)
        else:
            unreadable = True
    return frozenset(models), unreadable


def _scan_source(source: str, rel: str) -> tuple[list[str], list[str]]:
    """``(partial pack reads, unreadable options)`` in *source*, located at *rel*."""
    partial_reads: list[str] = []
    unreadable: list[str] = []
    for call in _option_calls(ast.parse(source)):
        models, has_unreadable_arg = _named_models(call)
        where = f"  {rel}:{call.lineno}"
        if has_unreadable_arg:
            unreadable.append(where)
        named_pack_models = sorted(models & PACK_MODELS)
        if named_pack_models and rel not in PARTIAL_PACK_READERS:
            partial_reads.append(f"{where}: {', '.join(named_pack_models)}")
    return partial_reads, unreadable


def _scan_app() -> tuple[list[str], list[str]]:
    partial_reads: list[str] = []
    unreadable: list[str] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        rel = path.relative_to(BACKEND_APP.parent).as_posix()
        file_reads, file_unreadable = _scan_source(path.read_text(encoding="utf-8"), rel)
        partial_reads.extend(file_reads)
        unreadable.extend(file_unreadable)
    return partial_reads, unreadable


def test_pack_models_are_read_partially_only_by_the_catalog_view() -> None:
    partial_reads, _ = _scan_app()
    assert not partial_reads, (
        "A partial read of the driver-pack models outside "
        "app/packs/services/catalog_view.py leaves columns deferred on rows that "
        "enter the session identity map. The MissingGreenlet does not land here: "
        "it lands on a later consumer, on another session, touching a column "
        "neither reader named. Read the Columns paragraph of load_pack_catalog's "
        "docstring (app/packs/services/catalog_view.py), then either select the "
        "full entity or add an entry to PARTIAL_PACK_READERS saying why the two "
        "readers' column sets are safe together:\n" + "\n".join(partial_reads)
    )


def test_loader_options_name_their_model_readably() -> None:
    _, unreadable = _scan_app()
    assert not unreadable, (
        "A load_only/defer option this scan cannot read: its arguments do not "
        "name a model as `Model.column`, so it cannot be told apart from a "
        "partial read of the driver-pack models. If this option has nothing to "
        "do with the driver-pack models, naming its columns inline is what lets "
        "the scan tell the difference. Spell the columns out, or extend "
        "_named_models to understand the new form — do not leave the "
        "scan guessing:\n" + "\n".join(unreadable)
    )


def test_the_scan_reports_a_partial_pack_read_however_it_is_wrapped() -> None:
    source = "stmt.options(\n    load_only(\n        DriverPackRelease.release,\n    ),\n)\n"
    partial_reads, unreadable = _scan_source(source, "app/somewhere/new_reader.py")
    assert partial_reads == ["  app/somewhere/new_reader.py:2: DriverPackRelease"]
    assert unreadable == []


def test_the_scan_passes_over_a_defer_on_an_unrelated_model() -> None:
    source = "stmt.options(selectinload(Device.appium_node).defer(AppiumNode.live_capabilities))\n"
    assert _scan_source(source, "app/devices/services/connectivity.py") == ([], [])


def test_the_scan_reports_an_option_it_cannot_read() -> None:
    source = "stmt.options(load_only(*columns))\n"
    partial_reads, unreadable = _scan_source(source, "app/somewhere/new_reader.py")
    assert partial_reads == []
    assert unreadable == ["  app/somewhere/new_reader.py:1"]


def test_the_allowlisted_reader_is_exempt_from_the_pack_finding() -> None:
    source = "stmt.options(load_only(DriverPack.state))\n"
    partial_reads, _ = _scan_source(source, "app/packs/services/catalog_view.py")
    assert partial_reads == []


def test_the_scan_reports_a_chained_partial_pack_read_outside_the_allowlist() -> None:
    source = "stmt.options(joinedload(DriverPack.releases).load_only(DriverPackRelease.release))\n"
    partial_reads, unreadable = _scan_source(source, "app/somewhere/new_reader.py")
    assert partial_reads == ["  app/somewhere/new_reader.py:1: DriverPackRelease"]
    assert unreadable == []
