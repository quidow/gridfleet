"""Pack-worker entrypoint: serve adapter hooks over JSON-lines stdio."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib.util
import os
import sys
import traceback
from typing import Any

import agent_app.pack.worker_protocol as wp


def _claim_stdio() -> Any:  # noqa: ANN401
    """Keep protocol stdout separate from adapter output."""
    proto_out = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    return proto_out


def _load_adapter(site: str) -> Any:  # noqa: ANN401
    init_py = os.path.join(site, "adapter", "__init__.py")
    spec = importlib.util.spec_from_file_location(
        "gridfleet_pack_adapter",
        init_py,
        submodule_search_locations=[os.path.join(site, "adapter")],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load adapter from {init_py}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["gridfleet_pack_adapter"] = module
    spec.loader.exec_module(module)
    cls = getattr(module, "Adapter", None)
    if cls is None:
        raise RuntimeError("adapter module does not expose class Adapter")
    return cls()


def _bind_call(adapter: Any, hook: str, payload: dict[str, Any]) -> Any:  # noqa: ANN401
    spec = wp.HOOK_SPECS[hook]
    if hook in {"pre_session", "post_session"}:
        session_spec = wp.decode_session_spec(payload["spec"])
        if hook == "pre_session":
            return adapter.pre_session(session_spec)
        return adapter.post_session(session_spec, wp.decode_session_outcome(payload["outcome"]))
    if hook == "lifecycle_action":
        return adapter.lifecycle_action(
            payload["action_id"],
            payload.get("args", {}),
            wp.decode_context(spec, payload),
        )
    return getattr(adapter, hook)(wp.decode_context(spec, payload))


async def _run_hook(adapter: Any, req: dict[str, Any], proto_out: Any, hook_timeout: float) -> None:  # noqa: ANN401
    req_id = req.get("id")
    hook = req.get("hook")
    if not isinstance(req_id, int):
        return
    spec = wp.HOOK_SPECS.get(hook) if isinstance(hook, str) else None
    if not isinstance(hook, str) or spec is None or not callable(getattr(adapter, hook, None)):
        response = wp.HookResponse(
            id=req_id,
            ok=False,
            error={"kind": "unknown_hook", "message": str(hook)},
        )
        print(wp.encode(response), file=proto_out)
        return
    try:
        result = await asyncio.wait_for(
            _bind_call(adapter, hook, req["payload"]),
            timeout=hook_timeout,
        )
        response = wp.HookResponse(id=req_id, ok=True, result=wp.as_jsonable(result))
    except TimeoutError:
        response = wp.HookResponse(
            id=req_id,
            ok=False,
            error={"kind": "timeout", "message": f"{hook} exceeded {hook_timeout}s"},
        )
    except Exception:
        response = wp.HookResponse(
            id=req_id,
            ok=False,
            error={"kind": "exception", "message": traceback.format_exc(limit=8)},
        )
    print(wp.encode(response), file=proto_out)


async def _serve(adapter: Any, proto_out: Any, hook_timeout: float) -> None:  # noqa: ANN401
    supported = sorted(hook for hook in wp.HOOK_SPECS if callable(getattr(adapter, hook, None)))
    env = adapter.subprocess_env() if callable(getattr(adapter, "subprocess_env", None)) else None
    tools = adapter.tool_versions() if callable(getattr(adapter, "tool_versions", None)) else {}
    print(
        wp.encode(
            wp.Handshake(
                supported_hooks=supported,
                subprocess_env=dataclasses.asdict(env) if env is not None else {},
                tool_versions=dict(tools),
            )
        ),
        file=proto_out,
    )

    reader = asyncio.StreamReader()
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader),
        sys.stdin,
    )
    tasks: set[asyncio.Task[None]] = set()
    try:
        while line := (await reader.readline()).decode():
            req = wp.decode_line(line)
            task = asyncio.create_task(_run_hook(adapter, req, proto_out, hook_timeout))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-id", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--hook-timeout", type=float, default=30.0)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    proto_out = _claim_stdio()
    try:
        adapter = _load_adapter(args.site)
        adapter.pack_id = args.pack_id
        adapter.pack_release = args.release
        asyncio.run(_serve(adapter, proto_out, args.hook_timeout))
    finally:
        proto_out.close()


if __name__ == "__main__":
    main()
