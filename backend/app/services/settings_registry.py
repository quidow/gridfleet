import sys
from typing import TYPE_CHECKING

from app.settings import registry as _registry

if TYPE_CHECKING:
    from app.settings.registry import *  # noqa: F403

sys.modules[__name__] = _registry
