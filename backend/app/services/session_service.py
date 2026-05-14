import sys

from app.sessions import service as _service
from app.sessions.service import *  # noqa: F403

sys.modules[__name__] = _service
