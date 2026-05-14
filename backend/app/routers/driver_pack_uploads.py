import sys
from typing import TYPE_CHECKING

from app.packs.routers import uploads as _uploads

if TYPE_CHECKING:
    from app.packs.routers.uploads import *  # noqa: F403

sys.modules[__name__] = _uploads
