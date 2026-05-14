"""Agent and backend AgentErrorCode enums must stay in sync."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_FILE = REPO_ROOT / "agent" / "agent_app" / "error_codes.py"
BACKEND_FILE = REPO_ROOT / "backend" / "app" / "agent_comm" / "error_codes.py"


def _load(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None, f"Cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_agent_error_code_enums_match() -> None:
    agent_mod = _load("_parity_agent_error_codes", AGENT_FILE)
    backend_mod = _load("_parity_backend_error_codes", BACKEND_FILE)
    agent = {member.name: member.value for member in agent_mod.AgentErrorCode}
    backend = {member.name: member.value for member in backend_mod.AgentErrorCode}
    assert agent == backend
