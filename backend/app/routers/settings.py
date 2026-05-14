import sys

from app.settings import router as _router

sys.modules[__name__] = _router
