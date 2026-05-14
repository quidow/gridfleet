import sys
from typing import TYPE_CHECKING

from app.packs.services import status as _status

if TYPE_CHECKING:
    from app.packs.services.status import *  # noqa: F403

sys.modules[__name__] = _status
