import sys
from typing import TYPE_CHECKING

from app.packs.services import lifecycle as _lifecycle

if TYPE_CHECKING:
    from app.packs.services.lifecycle import *  # noqa: F403

sys.modules[__name__] = _lifecycle
