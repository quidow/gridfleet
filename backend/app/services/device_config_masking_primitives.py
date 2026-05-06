from __future__ import annotations

import copy
import re
from typing import Any

SENSITIVE_PATTERNS = re.compile(r"(password|secret|key|token|credential)", re.IGNORECASE)
MASK_VALUE = "********"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge override into base, returning a new dict."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def mask_sensitive_by_pattern(config: dict[str, Any]) -> dict[str, Any]:
    """Mask values whose keys match sensitive patterns."""
    masked: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, dict):
            masked[key] = mask_sensitive_by_pattern(value)
        elif SENSITIVE_PATTERNS.search(key):
            masked[key] = MASK_VALUE
        else:
            masked[key] = value
    return masked
