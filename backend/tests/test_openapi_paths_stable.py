"""Lock the OpenAPI public surface across phased layout refactors.

The backend domain-layout refactor moves files around without touching
route definitions. This test asserts that every later phase produces
the same set of (path, method, operation_id) triples and the same
count. Any phase that intentionally changes the public surface must
update EXPECTED_PATH_COUNT and EXPECTED_FINGERPRINT in the same PR.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient

EXPECTED_PATH_COUNT: int | None = 149
EXPECTED_FINGERPRINT: str | None = "02fc06e1df7c09a9801808e88952ffe2b815645ca6b151e6f74de9f94b773f0c"


def _fingerprint(triples: list[tuple[str, str, str]]) -> str:
    serialized = json.dumps(sorted(triples), sort_keys=True).encode()
    return hashlib.sha256(serialized).hexdigest()


async def _collect_route_triples(client: AsyncClient) -> list[tuple[str, str, str]]:
    response = await client.get("/openapi.json")
    assert response.status_code == 200, response.text
    spec = response.json()
    triples: list[tuple[str, str, str]] = []
    for path, methods in spec.get("paths", {}).items():
        for method, details in methods.items():
            if method.lower() in {"parameters"}:
                continue
            op_id = details.get("operationId", "")
            triples.append((path, method.lower(), op_id))
    return triples


@pytest.mark.db
async def test_openapi_surface_unchanged(client: AsyncClient) -> None:
    triples = await _collect_route_triples(client)
    fingerprint = _fingerprint(triples)

    if EXPECTED_PATH_COUNT is None or EXPECTED_FINGERPRINT is None:
        print(f"\n[OPENAPI BASELINE]\n  count={len(triples)}\n  fingerprint={fingerprint}\n")
        pytest.skip(
            "Baseline not yet locked. Update EXPECTED_PATH_COUNT and "
            "EXPECTED_FINGERPRINT from the printed values and re-run."
        )

    assert len(triples) == EXPECTED_PATH_COUNT, (
        f"Route count changed: expected {EXPECTED_PATH_COUNT}, got {len(triples)}. "
        f"If this is intentional (a phase added or removed a route), update the constants."
    )
    assert fingerprint == EXPECTED_FINGERPRINT, (
        f"Route surface changed (path/method/operation_id triples differ). "
        f"Expected fingerprint {EXPECTED_FINGERPRINT}, got {fingerprint}. "
        f"If intentional, update the constants and explain in the PR body."
    )
