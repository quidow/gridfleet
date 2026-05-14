import sys

from app.analytics import router as _router

sys.modules[__name__] = _router
