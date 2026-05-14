import sys

from app.devices.services import bulk as _bulk
from app.devices.services.bulk import *  # noqa: F403

sys.modules[__name__] = _bulk
