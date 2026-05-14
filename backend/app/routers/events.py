import sys

from app.events import router as _router

sys.modules[__name__] = _router
