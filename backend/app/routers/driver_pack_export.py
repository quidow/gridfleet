"""Route for exporting a driver-pack release as a gzip tarball.

Endpoint:
    POST /api/driver-packs/{pack_id}/releases/{release}/export

Returns the tarball as ``application/gzip`` with:
  - ``X-Pack-Sha256``: hex-encoded SHA-256 of the returned bytes.
  - ``Content-Disposition``: ``attachment; filename=<safe-pack-id>-<release>.tar.gz``.

Admin-only (``Depends(require_admin)``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from app.config import settings
from app.database import get_db
from app.services.auth_dependencies import require_admin
from app.services.pack_export_service import export_pack
from app.services.pack_storage_service import PackStorageService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/driver-packs", tags=["driver-packs"])

_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9._-]")


def get_pack_storage() -> PackStorageService:
    """FastAPI dependency returning a :class:`PackStorageService` for the configured dir.

    Override ``app.dependency_overrides[get_pack_storage]`` in tests to point
    at a writable ``tmp_path``-rooted instance instead of the production dir.
    """
    return PackStorageService(root=settings.driver_pack_storage_dir)


def _safe_filename_segment(value: str) -> str:
    """Replace characters unsafe for a Content-Disposition filename."""
    return _UNSAFE_RE.sub("_", value)


@router.post(
    "/{pack_id}/releases/{release}/export",
    summary="Export a driver-pack release as a gzip tarball",
    status_code=status.HTTP_200_OK,
)
async def export_release(
    pack_id: str,
    release: str,
    _username: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    storage: PackStorageService = Depends(get_pack_storage),
) -> Response:
    """Export a driver-pack release as a ``.tar.gz`` tarball.

    When a stored artifact exists, the existing file is returned verbatim.
    Older rows without artifacts get a fresh tarball synthesised from
    ``manifest_json``.

    Args:
        pack_id: The pack identifier (may contain ``/`` for ``local/*`` packs).
        release: The SemVer release string.
        _username: Injected by ``require_admin``; confirms admin access.
        session: Database session.
        storage: Pack storage service instance.

    Returns:
        ``application/gzip`` response with ``X-Pack-Sha256`` and
        ``Content-Disposition`` headers set.

    Raises:
        404: When the pack+release combination is not found.
    """
    try:
        data, sha = await export_pack(session, storage, pack_id, release)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    safe_id = _safe_filename_segment(pack_id)
    safe_release = _safe_filename_segment(release)
    filename = f"{safe_id}-{safe_release}.tar.gz"

    return Response(
        content=data,
        media_type="application/gzip",
        headers={
            "X-Pack-Sha256": sha,
            "Content-Disposition": f"attachment; filename={filename}",
        },
    )
