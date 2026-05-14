import sys
from typing import TYPE_CHECKING

from app.packs.services import capability as _capability

if TYPE_CHECKING:
    from app.packs.services.capability import *  # noqa: F403

sys.modules[__name__] = _capability
