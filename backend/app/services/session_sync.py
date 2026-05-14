import sys

from app.sessions import service_sync as _service_sync
from app.sessions.service_sync import *  # noqa: F403

sys.modules[__name__] = _service_sync
