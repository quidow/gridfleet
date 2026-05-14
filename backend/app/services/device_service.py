import sys

from app.devices.services import service as _service
from app.devices.services.service import *  # noqa: F403

sys.modules[__name__] = _service
