import sys

from app.sessions import router as _router
from app.sessions.router import *  # noqa: F403

sys.modules[__name__] = _router
