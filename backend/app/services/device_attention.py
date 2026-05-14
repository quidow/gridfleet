import sys

from app.devices.services import attention as _attention
from app.devices.services.attention import *  # noqa: F403

sys.modules[__name__] = _attention
