"""Outbox delivery must never consult a PostgreSQL transaction id.

Two promotion gates shipped and were withdrawn inside a single implementation
cycle, each because an empirical probe contradicted a plausible-sounding
argument about transaction-id ordering: ``pg_snapshot_xmax`` is
``latestCompletedXid + 1``, so a running transaction can sit at or above the
captured horizon and be absent from ``xip_list`` as well; and a transaction
whose first write is the outbox INSERT evaluates the sequence default before
``heap_insert`` assigns an xid, so it can hold a row id with no xid at all.
Explicit gap tracking replaced the reasoning rather than adding a third argument
about it. This guard keeps it from creeping back -- including through a helper
that looks harmless in isolation.

Deleting the last of these also fixed two things by construction: the poller no
longer burns a transaction id per poll per worker, and it no longer raises on a
standby, where ``pg_current_xact_id()`` errors for the whole of recovery.
"""

from __future__ import annotations

from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

# Lowercased comparison: PostgreSQL identifiers are case-insensitive, so
# ``PG_CURRENT_XACT_ID()`` is the same call.
_BANNED_TOKENS = (
    "pg_snapshot_xmax",
    "pg_snapshot_xmin",
    "pg_current_snapshot",
    "pg_current_xact_id",
    "txid_current",
    "watermark_candidate",
)


def test_no_transaction_id_reasoning_under_app() -> None:
    offenders: dict[str, list[str]] = {}
    for path in APP_ROOT.rglob("*.py"):
        lowered = path.read_text().lower()
        hits = sorted(token for token in _BANNED_TOKENS if token in lowered)
        if hits:
            offenders[path.relative_to(APP_ROOT.parent).as_posix()] = hits

    assert not offenders, (
        "Transaction-id reasoning found under app/:\n  "
        + "\n  ".join(f"{path}: {', '.join(tokens)}" for path, tokens in sorted(offenders.items()))
        + "\n\nOutbox delivery advances its frontier unconditionally and records the ids a scan passed "
        "over (app/events/event_bus.py::_record_new_gaps). Gap resolution consults visibility only. If "
        "you need to know whether a row can still appear, look it up by id -- no transaction-id horizon "
        "can answer that question correctly."
    )
