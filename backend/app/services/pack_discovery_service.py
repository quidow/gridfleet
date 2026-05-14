import sys
from typing import TYPE_CHECKING

from app.packs.services import discovery as _discovery

if TYPE_CHECKING:
    from app.packs.services.discovery import *  # noqa: F403

sys.modules[__name__] = _discovery
