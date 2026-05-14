import sys
from typing import TYPE_CHECKING

from app.packs import schemas as _schemas

if TYPE_CHECKING:
    from app.packs.schemas import *  # noqa: F403

sys.modules[__name__] = _schemas
