import sys
from typing import TYPE_CHECKING

from app.packs.services import feature_dispatch as _feature_dispatch

if TYPE_CHECKING:
    from app.packs.services.feature_dispatch import *  # noqa: F403

sys.modules[__name__] = _feature_dispatch
