import sys
from typing import TYPE_CHECKING

from app.packs.services import platform_catalog as _platform_catalog

if TYPE_CHECKING:
    from app.packs.services.platform_catalog import *  # noqa: F403

sys.modules[__name__] = _platform_catalog
