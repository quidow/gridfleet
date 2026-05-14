import sys

from app.devices.services import data_cleanup as _data_cleanup
from app.devices.services.data_cleanup import *  # noqa: F403

sys.modules[__name__] = _data_cleanup
