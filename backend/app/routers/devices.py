import sys

from app.devices.routers import catalog as _catalog
from app.devices.routers.catalog import *  # noqa: F403

sys.modules[__name__] = _catalog
