from __future__ import annotations

import json
from typing import Any

from pydantic import RootModel, model_validator

TEST_DATA_MAX_BYTES = 64 * 1024


class TestDataPayload(RootModel[dict[str, Any]]):
    """Free-form operator-attached data exposed to testkit. Top-level object only."""

    @model_validator(mode="after")
    def _validate(self) -> TestDataPayload:
        if not isinstance(self.root, dict):
            raise ValueError("test_data must be a JSON object at the root")
        encoded = json.dumps(self.root).encode("utf-8")
        if len(encoded) > TEST_DATA_MAX_BYTES:
            raise ValueError(f"test_data payload exceeds {TEST_DATA_MAX_BYTES} bytes (got {len(encoded)})")
        return self
