import sys
from typing import TYPE_CHECKING

from app.analytics import service as _service

if TYPE_CHECKING:
    from app.analytics.service import *  # noqa: F403

sys.modules[__name__] = _service
