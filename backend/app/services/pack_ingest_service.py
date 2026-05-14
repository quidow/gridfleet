import sys
from typing import TYPE_CHECKING

from app.packs.services import ingest as _ingest

if TYPE_CHECKING:
    from app.packs.services.ingest import *  # noqa: F403

sys.modules[__name__] = _ingest
