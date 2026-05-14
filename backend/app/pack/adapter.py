import sys
from typing import TYPE_CHECKING

from app.packs import adapter as _adapter

if TYPE_CHECKING:
    from app.packs.adapter import *  # noqa: F403

sys.modules[__name__] = _adapter
