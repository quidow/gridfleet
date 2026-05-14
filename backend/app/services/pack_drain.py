import sys
from typing import TYPE_CHECKING

from app.packs.services import drain as _drain

if TYPE_CHECKING:
    from app.packs.services.drain import *  # noqa: F403

sys.modules[__name__] = _drain
