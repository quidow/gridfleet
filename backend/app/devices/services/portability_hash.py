import hashlib
import json

from app.devices.schemas.portability import ExportBundle


def canonical_bundle_json(bundle: ExportBundle) -> bytes:
    """Render the bundle to canonical JSON: sorted keys, no whitespace, UTF-8."""
    payload = bundle.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_bundle_hash(bundle: ExportBundle) -> str:
    digest = hashlib.sha256(canonical_bundle_json(bundle)).hexdigest()
    return f"sha256:{digest}"
