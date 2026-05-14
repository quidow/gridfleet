import sys
from typing import TYPE_CHECKING

from app.packs.services import service as _service

if TYPE_CHECKING:
    from app.packs.services.service import *  # noqa: F403

sys.modules[__name__] = _service
