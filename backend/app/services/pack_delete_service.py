import sys
from typing import TYPE_CHECKING

from app.packs.services import delete as _delete

if TYPE_CHECKING:
    from app.packs.services.delete import *  # noqa: F403

sys.modules[__name__] = _delete
