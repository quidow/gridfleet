import sys
from typing import TYPE_CHECKING

from app.settings import service as _service

if TYPE_CHECKING:
    from app.settings.service import *  # noqa: F403

sys.modules[__name__] = _service
