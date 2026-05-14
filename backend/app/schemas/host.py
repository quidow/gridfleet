import sys
from typing import TYPE_CHECKING

from app.hosts import schemas as _schemas

if TYPE_CHECKING:
    from app.hosts.schemas import *  # noqa: F403

sys.modules[__name__] = _schemas
