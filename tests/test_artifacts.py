from __future__ import annotations

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed

from app.rollbackready.artifacts import GcsArtifactStore, InMemoryArtifactStore
from app.rollbackready.errors import RollbackReadyError


class _Blob:
    def __init__(self, *, missing: bool = False, conflict: bool = False) -> None:
        self.generation: int | None = None
        self.missing = missing
        self.conflict = conflict
        self.deleted = False

    def upload_from_string(self, *_: object, **__: object) -> None:
        if self.conflict:
            raise PreconditionFailed("exists")

    def reload(self, **_: object) -> None:
        self.generation = 42

    def download_as_bytes(self, **_: object) -> bytes:
        if self.missing:
            raise NotFound("missing")
        return b"archive"

    def delete(self, **_: object) -> None:
        if self.missing:
            raise NotFound("missing")
        self.deleted = True


class _Bucket:
    def __init__(self) -> None:
        self.available = True
        self.blobs: dict[str, _Blob] = {}

    def blob(self, name: str, **_: object) -> _Blob:
        return self.blobs.setdefault(name, _Blob())

    def exists(self, **_: object) -> bool:
        return self.available


class _Client:
    bucket_instance = _Bucket()

    def __init__(self, **_: object) -> None:
        pass

    def bucket(self, _: str) -> _Bucket:
        return self.bucket_instance


def test_in_memory_store_rejects_conflict_and_generation_mismatch() -> None:
    store = InMemoryArtifactStore()
    stored = store.put("analysis", b"zip")

    assert store.get(stored.object_name, stored.generation) == b"zip"
    with pytest.raises(RollbackReadyError) as conflict:
        store.put("analysis", b"other")
    assert conflict.value.code == "ARTIFACT_CONFLICT"
    with pytest.raises(RollbackReadyError):
        store.get(stored.object_name, "wrong")
    store.delete(stored.object_name, "wrong")
    assert store.get(stored.object_name, "1") == b"zip"
    store.delete(stored.object_name)
    with pytest.raises(RollbackReadyError):
        store.get(stored.object_name, "1")
    store.check()


def test_gcs_store_uses_generation_preconditions_and_handles_missing(
    monkeypatch,
) -> None:
    _Client.bucket_instance = _Bucket()
    monkeypatch.setattr("app.rollbackready.artifacts.storage.Client", _Client)
    store = GcsArtifactStore("private-bucket")

    stored = store.put("analysis", b"archive")
    assert stored.generation == "42"
    assert store.get(stored.object_name, stored.generation) == b"archive"
    store.delete(stored.object_name, stored.generation)
    assert _Client.bucket_instance.blobs[stored.object_name].deleted
    store.check()

    missing = _Blob(missing=True)
    _Client.bucket_instance.blobs["missing"] = missing
    with pytest.raises(RollbackReadyError) as unavailable:
        store.get("missing", "1")
    assert unavailable.value.code == "RAW_ARTIFACTS_DELETED"
    store.delete("missing", "1")


def test_gcs_store_reports_conflict_and_failed_health(monkeypatch) -> None:
    _Client.bucket_instance = _Bucket()
    _Client.bucket_instance.blobs["analyses/conflict/bundle.zip"] = _Blob(
        conflict=True
    )
    monkeypatch.setattr("app.rollbackready.artifacts.storage.Client", _Client)
    store = GcsArtifactStore("private-bucket")

    with pytest.raises(RollbackReadyError) as conflict:
        store.put("conflict", b"archive")
    assert conflict.value.code == "ARTIFACT_CONFLICT"

    _Client.bucket_instance.available = False
    with pytest.raises(RuntimeError, match="unavailable"):
        store.check()
