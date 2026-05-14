import sys
from typing import TYPE_CHECKING

from app.packs.services import release_ordering as _release_ordering

if TYPE_CHECKING:
    from app.packs.services.release_ordering import *  # noqa: F403

sys.modules[__name__] = _release_ordering
