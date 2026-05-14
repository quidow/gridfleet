import sys
from typing import TYPE_CHECKING

from app.events import catalog as _catalog

if TYPE_CHECKING:
    from app.events.catalog import *  # noqa: F403

sys.modules[__name__] = _catalog
