import sys
from typing import TYPE_CHECKING

from app.packs.routers import templates as _templates

if TYPE_CHECKING:
    from app.packs.routers.templates import *  # noqa: F403

sys.modules[__name__] = _templates
