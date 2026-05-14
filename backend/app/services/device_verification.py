import sys

from app.devices.services import verification as _verification
from app.devices.services.verification import *  # noqa: F403

sys.modules[__name__] = _verification
