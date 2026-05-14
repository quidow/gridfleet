import sys

from app.devices.services import intent_evaluator as _intent_evaluator
from app.devices.services.intent_evaluator import *  # noqa: F403

sys.modules[__name__] = _intent_evaluator
