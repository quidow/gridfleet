import sys

from app.plugins import router as _router

sys.modules[__name__] = _router
