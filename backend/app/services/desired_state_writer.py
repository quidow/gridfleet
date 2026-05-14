import sys

from app.appium_nodes.services import desired_state_writer as _desired_state_writer
from app.appium_nodes.services.desired_state_writer import *  # noqa: F403

sys.modules[__name__] = _desired_state_writer
