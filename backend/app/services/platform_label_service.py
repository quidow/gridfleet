import sys

from app.devices.services import platform_label as _platform_label
from app.devices.services.platform_label import *  # noqa: F403

sys.modules[__name__] = _platform_label
