import sys
from typing import TYPE_CHECKING

from app.grid import service as _service

if TYPE_CHECKING:
    from app.grid.service import *  # noqa: F403

sys.modules[__name__] = _service
