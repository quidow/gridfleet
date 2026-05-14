import sys

from app.devices.services import identity as _identity
from app.devices.services.identity import *  # noqa: F403

sys.modules[__name__] = _identity
