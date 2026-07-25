from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def test_router_builds_from_router_dockerfile() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    for compose_file in ("docker-compose.yml", "docker-compose.prod.yml"):
        compose = yaml.safe_load((repo_root / "docker" / compose_file).read_text())
        build = compose["services"]["router"]["build"]

        assert build["dockerfile"] == "router/Dockerfile"


def test_host_docker_internal_is_resolvable_by_manager_and_router() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    expected_host = "host.docker.internal:host-gateway"

    for compose_file in ("docker-compose.yml", "docker-compose.prod.yml"):
        compose = yaml.safe_load((repo_root / "docker" / compose_file).read_text())

        for service_name in ("backend", "router"):
            service = compose["services"][service_name]

            assert expected_host in service.get("extra_hosts", [])


_IDLE_TIMEOUT_FLAG = "idle_in_transaction_session_timeout"


def test_the_command_parser_rejects_a_string_command_loudly() -> None:
    """Compose permits ``command:`` as a single string as well as a list.

    A string silently yields an empty flag dict, and the caller then fails on a
    missing-flag message that sends the reader hunting for a deleted setting
    instead of a changed YAML shape. Fail on the shape.
    """
    with pytest.raises(TypeError, match="list"):
        _flags_from_command("postgres -c idle_in_transaction_session_timeout=60s")


def _flags_from_command(command: object) -> dict[str, str]:
    """Parse ``-c key=value`` tokens out of a compose ``command:`` list."""
    if not isinstance(command, list):
        raise TypeError(
            f"postgres `command:` must be a YAML list for these flags to be readable, got {type(command).__name__}. "
            "Compose also accepts a single string; if it was changed to one, this parser needs updating -- the "
            "settings below have not necessarily been removed."
        )
    flags: dict[str, str] = {}
    for index, token in enumerate(command):
        if token == "-c" and index + 1 < len(command) and "=" in command[index + 1]:
            key, _, value = command[index + 1].partition("=")
            flags[key] = value
    return flags


def _postgres_command_flags(compose_file: str) -> dict[str, str]:
    """Parse the postgres service's ``-c key=value`` flags into a dict."""
    repo_root = Path(__file__).resolve().parents[3]
    compose = yaml.safe_load((repo_root / "docker" / compose_file).read_text())
    return _flags_from_command(compose["services"]["postgres"].get("command") or [])


def _seconds(raw: str) -> float:
    """Postgres duration GUC value -> seconds. A bare number is milliseconds."""
    if raw.endswith("ms"):
        return float(raw[:-2]) / 1000
    if raw.endswith("min"):
        return float(raw[:-3]) * 60
    if raw.endswith("s"):
        return float(raw[:-1])
    return float(raw) / 1000


def test_postgres_bounds_idle_in_transaction_sessions() -> None:
    """Both stacks must cap idle-in-transaction sessions, at the same value.

    The outbox poller retires an unresolved gap id on a time bound derived from
    this setting (``GAP_RETIREMENT_SEC``). Unset, the default is 0 -- unlimited --
    and a session could hold a ``system_events`` sequence value forever, which
    would make any fixed retirement constant arbitrary.
    """
    values: dict[str, str] = {}
    for compose_file in ("docker-compose.yml", "docker-compose.prod.yml"):
        flags = _postgres_command_flags(compose_file)
        assert _IDLE_TIMEOUT_FLAG in flags, (
            f"{compose_file}: postgres service does not set {_IDLE_TIMEOUT_FLAG}; "
            "the outbox gap retirement bound is derived from it"
        )
        values[compose_file] = flags[_IDLE_TIMEOUT_FLAG]

    assert len(set(values.values())) == 1, f"dev and prod disagree on {_IDLE_TIMEOUT_FLAG}: {values}"
    assert _seconds(next(iter(values.values()))) >= 60, "an idle-transaction bound under 60s will abort slow tests"


def test_gap_retirement_is_derived_from_the_idle_transaction_bound() -> None:
    """``GAP_RETIREMENT_SEC`` is derived from Postgres config, not chosen.

    Retiring a gap early strands an event row. The only thing that genuinely
    bounds how long a ``system_events`` sequence value can stay unresolved is how
    long a transaction can stay open, which is what the compose setting caps --
    so if someone raises or lowers that setting, this test makes them move the
    constant with it.
    """
    from app.events.event_bus import (
        GAP_RETIREMENT_SAFETY_MULTIPLE,
        GAP_RETIREMENT_SEC,
        IDLE_IN_TRANSACTION_BOUND_SEC,
    )

    compose_bound = _seconds(_postgres_command_flags("docker-compose.yml")[_IDLE_TIMEOUT_FLAG])

    assert compose_bound == IDLE_IN_TRANSACTION_BOUND_SEC, (
        "app/events/event_bus.py mirrors the compose idle-in-transaction bound; they have drifted"
    )
    assert GAP_RETIREMENT_SEC == IDLE_IN_TRANSACTION_BOUND_SEC * GAP_RETIREMENT_SAFETY_MULTIPLE
    assert GAP_RETIREMENT_SAFETY_MULTIPLE >= 2.0, "the retirement bound must keep headroom over the raw timeout"
