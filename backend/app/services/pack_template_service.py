import sys
from typing import TYPE_CHECKING

from app.packs.services import template as _template

if TYPE_CHECKING:
    from app.packs.services.template import *  # noqa: F403

sys.modules[__name__] = _template
