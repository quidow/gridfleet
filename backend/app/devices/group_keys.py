import re
from typing import Annotated

from pydantic import StringConstraints

# The W3C capability namespace a routing request uses to name a device group:
# ``gridfleet:group:<key>``. Lives with the key grammar it prefixes so both the
# grid matcher and the Appium-node stereotype builder can import it without
# either layer depending on the other.
GROUP_PREFIX = "gridfleet:group:"

GROUP_KEY_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"
GROUP_KEY_RE = re.compile(GROUP_KEY_PATTERN)
GroupKey = Annotated[str, StringConstraints(pattern=GROUP_KEY_PATTERN)]


def is_valid_group_key(value: str) -> bool:
    return GROUP_KEY_RE.fullmatch(value) is not None
