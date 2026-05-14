import sys
from typing import TYPE_CHECKING

from app.packs.services import host_compatibility as _host_compatibility

if TYPE_CHECKING:
    from app.packs.services.host_compatibility import *  # noqa: F403

sys.modules[__name__] = _host_compatibility
