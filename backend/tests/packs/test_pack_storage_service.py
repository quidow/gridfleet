from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.packs.services.storage import PackStorageError, PackStorageService


def test_store_writes_tarball_and_returns_sha(tmp_path: Path) -> None:
    svc = PackStorageService(root=tmp_path)
    payload = b"tarball-bytes"
    expected = hashlib.sha256(payload).hexdigest()
    record = svc.store(pack_id="vendor-foo", release="0.1.0", data=payload)
    assert record.sha256 == expected
    assert Path(record.path).read_bytes() == payload
    assert record.path.startswith(str(tmp_path))


def test_open_streams_existing_tarball(tmp_path: Path) -> None:
    svc = PackStorageService(root=tmp_path)
    record = svc.store(pack_id="vendor-foo", release="0.1.0", data=b"x")
    with svc.open(record.path) as handle:
        assert handle.read() == b"x"


def test_collision_replaces_when_sha_matches(tmp_path: Path) -> None:
    svc = PackStorageService(root=tmp_path)
    a = svc.store(pack_id="vendor-foo", release="0.1.0", data=b"same")
    b = svc.store(pack_id="vendor-foo", release="0.1.0", data=b"same")
    assert a.path == b.path
    assert a.sha256 == b.sha256


def test_collision_with_different_sha_raises(tmp_path: Path) -> None:
    svc = PackStorageService(root=tmp_path)
    svc.store(pack_id="vendor-foo", release="0.1.0", data=b"first")
    with pytest.raises(PackStorageError, match="hash mismatch"):
        svc.store(pack_id="vendor-foo", release="0.1.0", data=b"second")
