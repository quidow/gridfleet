import sys

from app.devices.services import groups as _groups
from app.devices.services.groups import *  # noqa: F403

sys.modules[__name__] = _groups
