"""Cross-component timeout-lattice rows that no compile step can check.

Both rows here are in the timeout-lattice table (docs/reference/architecture.md),
which is the single home for budget rules spanning two components:

* The agent's HTTP keep-alive default must exceed the backend agent-pool idle
  expiry: if the agent-side keep-alive is shorter, the backend pool hands out
  connections the agent already closed and non-idempotent calls fail with
  RemoteProtocolError.
* The testkit's per-attempt preparation-failure timeout must exceed the backend's
  ``request_timeout_sec``: below it, the client abandons the request before the
  ASGI watchdog can return its classified response, and the retry becomes a blind
  re-send against a transaction that is still in flight.

Both read the other component's value out of its source file. Neither adds a
package dependency from the backend onto that component.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from app.agent_comm.http_pool import POOL_KEEPALIVE_EXPIRY_SEC
from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_CONFIG_FILE = REPO_ROOT / "agent" / "agent_app" / "config.py"
TESTKIT_PACKAGE_DIR = REPO_ROOT / "testkit" / "gridfleet_testkit"


def _load(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None, f"Cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_testkit_client() -> ModuleType:
    """Load ``testkit/gridfleet_testkit/client.py`` by file path, as above.

    ``client.py`` reaches its siblings through relative imports, so a synthetic
    parent package carrying the testkit source directory on ``__path__`` is
    registered first. The real ``gridfleet_testkit/__init__`` is never executed:
    it pulls in Appium, which the backend venv does not have and must not gain.
    """
    package_name = "_parity_testkit"
    package = ModuleType(package_name)
    package.__path__ = [str(TESTKIT_PACKAGE_DIR)]
    sys.modules[package_name] = package
    return _load(f"{package_name}.client", TESTKIT_PACKAGE_DIR / "client.py")


def test_agent_keepalive_exceeds_backend_pool_idle() -> None:
    agent_config = _load("_parity_agent_config", AGENT_CONFIG_FILE)
    field = agent_config.CoreSettings.model_fields["http_keepalive_timeout_sec"]
    assert field.default > POOL_KEEPALIVE_EXPIRY_SEC


def test_testkit_preparation_failure_timeout_exceeds_backend_request_timeout() -> None:
    testkit_timeout = _load_testkit_client().PREPARATION_FAILURE_TIMEOUT_SEC
    backend_watchdog = Settings.model_fields["request_timeout_sec"].default
    assert testkit_timeout > backend_watchdog
