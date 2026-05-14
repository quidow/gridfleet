import sys
from typing import TYPE_CHECKING

from app.packs.services import upload as _upload

if TYPE_CHECKING:
    from app.packs.services.upload import *  # noqa: F403

sys.modules[__name__] = _upload
