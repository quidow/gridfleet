import sys
from typing import TYPE_CHECKING

from app.packs.services import feature_status as _feature_status

if TYPE_CHECKING:
    from app.packs.services.feature_status import *  # noqa: F403

sys.modules[__name__] = _feature_status
