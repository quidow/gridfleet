import random
from datetime import UTC, datetime

from app.seeding.context import SeedContext
from tests.seeding.helpers import build_test_seed_context, no_persistence_session


def test_seed_context_deterministic_rng() -> None:
    a = build_test_seed_context(seed=42)
    b = build_test_seed_context(seed=42)
    assert [a.rng.random() for _ in range(5)] == [b.rng.random() for _ in range(5)]


def test_seed_context_now_is_frozen() -> None:
    ctx = build_test_seed_context(seed=1)
    assert ctx.now.tzinfo is UTC
    assert isinstance(ctx.now, datetime)
    first = ctx.now
    second = ctx.now
    assert first is second


def test_seed_context_requires_session_to_persist() -> None:
    ctx = SeedContext(session=no_persistence_session(), rng=random.Random(0), now=datetime.now(UTC))
    assert ctx.session is None
