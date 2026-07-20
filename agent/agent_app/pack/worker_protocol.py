"""The adapter-hook wire contract between the agent and its pack workers.

Both the supervisor and worker import this module, so the two sides cannot
drift. It intentionally uses only the standard library plus the agent's pack
data modules because this module is also imported by the worker subprocess.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import agent_app.pack.adapter_types as t
import agent_app.pack.contexts as c

if TYPE_CHECKING:
    from collections.abc import Callable

PROTOCOL_VERSION = 1


@dataclass
class Handshake:
    supported_hooks: list[str]
    subprocess_env: dict[str, Any]
    tool_versions: dict[str, str | None]
    protocol_version: int = PROTOCOL_VERSION


@dataclass
class HookRequest:
    id: int
    hook: str
    payload: dict[str, Any]


@dataclass
class HookResponse:
    id: int
    ok: bool
    result: Any = None
    error: dict[str, str] | None = None


@dataclass
class HookSpec:
    ctx_type: type[Any] | None
    decode_result: Callable[[Any], Any]
    expected: type[Any]


def _decode_field_errors(raw: Any) -> list[t.FieldError]:  # noqa: ANN401
    return [t.FieldError(**item) for item in raw]


def _decode_discovery_candidates(raw: Any) -> list[t.DiscoveryCandidate]:  # noqa: ANN401
    return [
        t.DiscoveryCandidate(**{**item, "field_errors": _decode_field_errors(item.get("field_errors", []))})
        for item in raw
    ]


def _decode_normalized_device(raw: Any) -> t.NormalizedDevice:  # noqa: ANN401
    return t.NormalizedDevice(**{**raw, "field_errors": _decode_field_errors(raw.get("field_errors", []))})


def _decode_health_results(raw: Any) -> list[t.HealthCheckResult]:  # noqa: ANN401
    return [t.HealthCheckResult(**item) for item in raw]


def _decode_doctor_results(raw: Any) -> list[t.DoctorCheckResult]:  # noqa: ANN401
    return [t.DoctorCheckResult(**item) for item in raw]


def _decode_lifecycle_result(raw: Any) -> t.LifecycleActionResult:  # noqa: ANN401
    return t.LifecycleActionResult(**raw)


HOOK_SPECS: dict[str, HookSpec] = {
    "discover": HookSpec(c.DiscoveryCtx, _decode_discovery_candidates, list),
    "normalize_device": HookSpec(c.NormalizeCtx, _decode_normalized_device, t.NormalizedDevice),
    "health_check": HookSpec(c.HealthCtx, _decode_health_results, list),
    "doctor": HookSpec(c.DoctorCtx, _decode_doctor_results, list),
    "lifecycle_action": HookSpec(c.LifecycleCtx, _decode_lifecycle_result, t.LifecycleActionResult),
    "pre_session": HookSpec(None, dict, dict),
    "post_session": HookSpec(None, lambda _raw: None, type(None)),
}


def encode(obj: Any) -> str:  # noqa: ANN401
    """Encode a protocol object or JSON-safe value as one compact line."""
    value = asdict(obj) if hasattr(obj, "__dataclass_fields__") else obj
    return json.dumps(value, separators=(",", ":"))


def encode_request(req_id: int, hook: str, payload: dict[str, Any]) -> str:
    return encode(HookRequest(id=req_id, hook=hook, payload=payload))


def decode_line(line: str) -> dict[str, Any]:
    parsed = json.loads(line)
    if not isinstance(parsed, dict):
        raise ValueError(f"protocol line is not an object: {line[:200]!r}")
    return parsed


def decode_context(spec: HookSpec, payload: dict[str, Any]) -> Any:  # noqa: ANN401
    """Rebuild the context carried by a hook payload in the worker."""
    if spec.ctx_type is None:
        raise ValueError("hook does not carry a context")
    return spec.ctx_type(**payload["ctx"])


def decode_session_spec(raw: dict[str, Any]) -> t.SessionSpec:
    return t.SessionSpec(**raw)


def decode_session_outcome(raw: dict[str, Any]) -> t.SessionOutcome:
    return t.SessionOutcome(**raw)


def as_jsonable(value: Any) -> Any:  # noqa: ANN401
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, list):
        return [as_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: as_jsonable(item) for key, item in value.items()}
    return value
