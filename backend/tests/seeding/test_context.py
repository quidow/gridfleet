from datetime import UTC, datetime

from app.seeding.context import SeedContext


def test_seed_context_deterministic_rng() -> None:
    a = SeedContext.build(session=None, seed=42)  # type: ignore[arg-type]
    b = SeedContext.build(session=None, seed=42)  # type: ignore[arg-type]
    assert [a.rng.random() for _ in range(5)] == [b.rng.random() for _ in range(5)]


def test_seed_context_now_is_frozen() -> None:
    ctx = SeedContext.build(session=None, seed=1)  # type: ignore[arg-type]
    assert ctx.now.tzinfo is UTC
    assert isinstance(ctx.now, datetime)
    # Same instance exposed each access — the context freezes the clock.
    first = ctx.now
    second = ctx.now
    assert first is second


def test_seed_context_requires_session_to_persist(monkeypatch) -> None:  # noqa: ANN001
    ctx = SeedContext(session=None, rng=__import__("random").Random(0), now=datetime.now(UTC))  # type: ignore[arg-type]
    assert ctx.session is None  # sanity; persistence tests live with factories
