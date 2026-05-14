import sys
from typing import TYPE_CHECKING

from app.settings import service_config as _service_config

if TYPE_CHECKING:
    from app.settings.service_config import *  # noqa: F403

sys.modules[__name__] = _service_config
