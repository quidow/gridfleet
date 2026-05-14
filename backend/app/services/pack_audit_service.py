import sys
from typing import TYPE_CHECKING

from app.packs.services import audit as _audit

if TYPE_CHECKING:
    from app.packs.services.audit import *  # noqa: F403

sys.modules[__name__] = _audit
