import hashlib
import json
from datetime import UTC

from app.devices.schemas.portability import ExportBundle


def canonical_bundle_json(bundle: ExportBundle) -> bytes:
    """Render the bundle to canonical JSON: sorted keys, no whitespace, UTF-8.

    Normalises ``exported_at`` to UTC so equivalent instants in different offset
    forms (``Z`` vs ``+00:00`` vs ``-04:00``) hash identically.
    """
    payload = bundle.model_dump(mode="json")
    if bundle.exported_at.tzinfo is None:
        # Naive datetimes are treated as UTC for hashing purposes.
        utc_dt = bundle.exported_at.replace(tzinfo=UTC)
    else:
        utc_dt = bundle.exported_at.astimezone(UTC)
    payload["exported_at"] = utc_dt.isoformat().replace("+00:00", "Z")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_bundle_hash(bundle: ExportBundle) -> str:
    """Return ``sha256:<hex>`` of the canonical JSON for ``bundle``."""
    digest = hashlib.sha256(canonical_bundle_json(bundle)).hexdigest()
    return f"sha256:{digest}"
