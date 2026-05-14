import sys

from app.devices.routers import verification as _verification
from app.devices.routers.verification import *  # noqa: F403

sys.modules[__name__] = _verification
