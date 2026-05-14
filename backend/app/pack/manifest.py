import sys
from typing import TYPE_CHECKING

from app.packs import manifest as _manifest

if TYPE_CHECKING:
    from app.packs.manifest import *  # noqa: F403

sys.modules[__name__] = _manifest
