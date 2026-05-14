import sys

from app.devices.routers import bulk as _bulk
from app.devices.routers.bulk import *  # noqa: F403

sys.modules[__name__] = _bulk
