import sys
from typing import TYPE_CHECKING

from app.packs.routers import export as _export

if TYPE_CHECKING:
    from app.packs.routers.export import *  # noqa: F403

sys.modules[__name__] = _export
