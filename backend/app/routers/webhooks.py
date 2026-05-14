import sys

from app.webhooks import router as _router

sys.modules[__name__] = _router
