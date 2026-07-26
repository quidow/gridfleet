from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class HasRelease(Protocol):
    # Read-only on purpose: a mutable protocol member is invariant, which a
    # frozen dataclass release (app.packs.services.catalog_view.PackReleaseView)
    # cannot satisfy. The ORM row satisfies a read-only member just as well.
    @property
    def release(self) -> str: ...


_FALLBACK = (0,)


def parse_release_key(release: str) -> tuple[int, ...]:
    parts: list[int] = []
    for segment in release.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            return _FALLBACK
    return tuple(parts) if parts else _FALLBACK


def latest_release[T: HasRelease](releases: Sequence[T]) -> T | None:
    if not releases:
        return None
    return max(releases, key=lambda r: parse_release_key(r.release))


def selected_release[T: HasRelease](releases: Sequence[T], current_release: str | None = None) -> T | None:
    if current_release:
        for release in releases:
            if release.release == current_release:
                return release
    return latest_release(releases)
