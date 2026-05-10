from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from starlette.requests import Request


async def status(_request: Request) -> JSONResponse:
    return JSONResponse({"value": {"ready": True, "message": "fixture appium stub"}})


app = Starlette(routes=[Route("/status", status, methods=["GET"])])
