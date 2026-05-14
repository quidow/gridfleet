import sys

from app.devices.services import connectivity as _connectivity
from app.devices.services.connectivity import *  # noqa: F403

sys.modules[__name__] = _connectivity
