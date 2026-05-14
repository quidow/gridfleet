import sys

from app.devices.services import identity_conflicts as _identity_conflicts
from app.devices.services.identity_conflicts import *  # noqa: F403

sys.modules[__name__] = _identity_conflicts
