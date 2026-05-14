import sys

from app.grid import router as _router

sys.modules[__name__] = _router
