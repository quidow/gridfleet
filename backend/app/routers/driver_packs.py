import sys
from typing import TYPE_CHECKING

from app.packs.routers import catalog as _catalog

if TYPE_CHECKING:
    from app.packs.routers.catalog import *  # noqa: F403

sys.modules[__name__] = _catalog
