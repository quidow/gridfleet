import sys
from typing import TYPE_CHECKING

from app.packs.services import start_shim as _start_shim

if TYPE_CHECKING:
    from app.packs.services.start_shim import *  # noqa: F403

sys.modules[__name__] = _start_shim
