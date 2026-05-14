import sys

from app.devices.services import test_data as _test_data
from app.devices.services.test_data import *  # noqa: F403

sys.modules[__name__] = _test_data
