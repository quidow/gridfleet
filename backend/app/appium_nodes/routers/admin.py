"""Admin endpoints for managed Appium node rows."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/admin/appium-nodes", tags=["admin"])
