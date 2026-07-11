"""Backend<->agent timeout-lattice rows that no compile step can check.

The agent's HTTP keep-alive default must exceed the backend agent-pool idle
expiry (timeout-lattice table, docs/reference/architecture.md): if the
agent-side keep-alive is shorter, the backend pool hands out connections the
agent already closed and non-idempotent calls fail with RemoteProtocolError.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from app.agent_comm.http_pool import POOL_KEEPALIVE_EXPIRY_SEC

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_CONFIG_FILE = REPO_ROOT / "agent" / "agent_app" / "config.py"


def _load(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None, f"Cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_agent_keepalive_exceeds_backend_pool_idle() -> None:
    agent_config = _load("_parity_agent_config", AGENT_CONFIG_FILE)
    field = agent_config.CoreSettings.model_fields["http_keepalive_timeout_sec"]
    assert field.default > POOL_KEEPALIVE_EXPIRY_SEC
