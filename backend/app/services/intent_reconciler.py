import sys

from app.devices.services import intent_reconciler as _intent_reconciler
from app.devices.services.intent_reconciler import *  # noqa: F403

sys.modules[__name__] = _intent_reconciler
