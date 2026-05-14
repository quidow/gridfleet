import sys

from app.runs import router as _router
from app.runs.router import *  # noqa: F403

sys.modules[__name__] = _router
