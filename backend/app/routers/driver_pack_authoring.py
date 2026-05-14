import sys
from typing import TYPE_CHECKING

from app.packs.routers import authoring as _authoring

if TYPE_CHECKING:
    from app.packs.routers.authoring import *  # noqa: F403

sys.modules[__name__] = _authoring
