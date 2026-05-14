import sys

from app.appium_nodes.services import resource_service as _resource_service
from app.appium_nodes.services.resource_service import *  # noqa: F403

sys.modules[__name__] = _resource_service
