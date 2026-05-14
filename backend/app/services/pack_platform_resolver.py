import sys
from typing import TYPE_CHECKING

from app.packs.services import platform_resolver as _platform_resolver

if TYPE_CHECKING:
    from app.packs.services.platform_resolver import *  # noqa: F403

sys.modules[__name__] = _platform_resolver
