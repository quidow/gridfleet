import sys
from typing import TYPE_CHECKING

from app.packs.routers import host_features as _host_features

if TYPE_CHECKING:
    from app.packs.routers.host_features import *  # noqa: F403

sys.modules[__name__] = _host_features
