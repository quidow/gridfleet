import sys
from typing import TYPE_CHECKING

from app.hosts import router as _router

if TYPE_CHECKING:
    from app.hosts.router import *  # noqa: F403

sys.modules[__name__] = _router
