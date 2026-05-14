"""Route exposing the per-host driver-pack feature-action dispatch.

Endpoint:
    POST /api/hosts/{host_id}/driver-packs/{pack_id}/features/{feature_id}/actions/{action_id}

Body: ``{"args": {...}}``. Response: ``FeatureActionResultOut``.

Admin-only (``Depends(require_admin)``). Pack feature lookups, agent HTTP
forwarding, and webhook recording all live in
:mod:`app.services.pack_feature_dispatch_service` so this router stays a thin
HTTP shim.
"""

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.dependencies import AdminDep, DbDep
from app.services.pack_feature_dispatch_service import dispatch_feature_action

router = APIRouter(prefix="/api/hosts", tags=["driver-pack-feature-actions"])


class FeatureActionRequest(BaseModel):
    """Body for the feature-action route — only ``args`` for the adapter."""

    args: dict[str, Any] = Field(default_factory=dict)


class FeatureActionResultOut(BaseModel):
    """HTTP-shaped response mirroring :class:`app.pack.adapter.FeatureActionResult`."""

    ok: bool
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


@router.post(
    "/{host_id}/driver-packs/{pack_id:path}/features/{feature_id}/actions/{action_id}",
    response_model=FeatureActionResultOut,
    summary="Invoke a driver-pack feature action on a host",
)
async def invoke_feature_action(
    host_id: uuid.UUID,
    pack_id: str,
    feature_id: str,
    action_id: str,
    body: FeatureActionRequest,
    _username: AdminDep,
    session: DbDep,
) -> FeatureActionResultOut:
    """Dispatch a feature action to the agent owning ``host_id``.

    Returns 404 when the host, pack, or feature can't be resolved, and 502
    when the agent fails to respond — both raised by the dispatcher.
    """
    result = await dispatch_feature_action(
        session,
        host_id=host_id,
        pack_id=pack_id,
        feature_id=feature_id,
        action_id=action_id,
        args=body.args,
    )
    await session.commit()
    return FeatureActionResultOut(ok=result.ok, detail=result.detail, data=result.data)
