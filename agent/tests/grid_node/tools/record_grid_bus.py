from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import zmq
import zmq.asyncio


def encode_frames(frames: list[bytes]) -> list[str]:
    return [base64.b64encode(frame).decode("ascii") for frame in frames]


def decode_frames(frames_b64: list[str]) -> list[bytes]:
    return [base64.b64decode(frame) for frame in frames_b64]


def _decoded_payload(frames: list[bytes]) -> object | None:
    for frame in reversed(frames):
        try:
            value = json.loads(frame.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def write_record(path: Path, *, ts: float, frames: list[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "ts": ts,
        "frames_b64": encode_frames(frames),
        "decoded": _decoded_payload(frames),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


async def run_observer(*, connect: str, out: Path, label: str) -> None:
    context = zmq.asyncio.Context.instance()
    socket = context.socket(zmq.SUB)
    socket.setsockopt(zmq.SUBSCRIBE, b"")
    socket.connect(connect)
    target = out / f"bus_{label}.jsonl"
    while True:
        frames = await socket.recv_multipart()
        write_record(target, ts=time.time(), frames=list(frames))


async def run_tap(*, listen: str, forward: str, out: Path, label: str) -> None:
    context = zmq.asyncio.Context.instance()
    incoming = context.socket(zmq.SUB)
    incoming.setsockopt(zmq.SUBSCRIBE, b"")
    incoming.bind(listen)
    outgoing = context.socket(zmq.PUB)
    outgoing.connect(forward)
    target = out / f"bus_{label}.jsonl"
    while True:
        frames = await incoming.recv_multipart()
        frame_list = list(frames)
        write_record(target, ts=time.time(), frames=frame_list)
        await outgoing.send_multipart(frame_list)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["observer", "tap"], required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--label", choices=["hub_to_node", "node_to_hub"], required=True)
    parser.add_argument("--connect")
    parser.add_argument("--listen")
    parser.add_argument("--forward")
    return parser


async def amain(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.mode == "observer":
        if args.connect is None:
            raise SystemExit("--connect is required for observer mode")
        await run_observer(connect=args.connect, out=args.out, label=args.label)
    else:
        if args.listen is None or args.forward is None:
            raise SystemExit("--listen and --forward are required for tap mode")
        await run_tap(listen=args.listen, forward=args.forward, out=args.out, label=args.label)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
