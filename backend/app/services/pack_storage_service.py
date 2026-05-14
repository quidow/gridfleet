import sys
from typing import TYPE_CHECKING

from app.packs.services import storage as _storage

if TYPE_CHECKING:
    from app.packs.services.storage import *  # noqa: F403

sys.modules[__name__] = _storage
