import sys
from typing import TYPE_CHECKING

from app.webhooks import service as _service

if TYPE_CHECKING:
    from app.webhooks.service import *  # noqa: F403

sys.modules[__name__] = _service
