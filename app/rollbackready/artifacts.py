from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Protocol

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage

from app.core.config import settings
from app.rollbackready.errors import RollbackReadyError


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    object_name: str
    generation: str
    expires_at: datetime


class ArtifactStore(Protocol):
    def put(self, analysis_id: str, archive: bytes) -> StoredArtifact: ...

    def get(self, object_name: str, generation: str) -> bytes: ...

    def delete(self, object_name: str, generation: str | None = None) -> None: ...

    def check(self) -> None: ...


class InMemoryArtifactStore:
    """Local/test implementation with the same generation semantics as GCS."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[str, bytes]] = {}
        self._lock = RLock()

    def put(self, analysis_id: str, archive: bytes) -> StoredArtifact:
        object_name = f"analyses/{analysis_id}/bundle.zip"
        generation = "1"
        with self._lock:
            if object_name in self._objects:
                raise RollbackReadyError(
                    "ARTIFACT_CONFLICT",
                    "The analysis artifact already exists.",
                    status_code=409,
                    analysis_id=analysis_id,
                )
            self._objects[object_name] = (generation, bytes(archive))
        return StoredArtifact(
            object_name=object_name,
            generation=generation,
            expires_at=datetime.now(UTC)
            + timedelta(hours=settings.rollbackready_artifact_retention_hours),
        )

    def get(self, object_name: str, generation: str) -> bytes:
        with self._lock:
            item = self._objects.get(object_name)
            if item is None or item[0] != generation:
                raise _unavailable()
            return item[1]

    def delete(self, object_name: str, generation: str | None = None) -> None:
        with self._lock:
            item = self._objects.get(object_name)
            if item is not None and (generation is None or item[0] == generation):
                self._objects.pop(object_name, None)

    def check(self) -> None:
        return None


class GcsArtifactStore:
    def __init__(self, bucket_name: str) -> None:
        self._client = storage.Client(project=settings.google_cloud_project)
        self._bucket = self._client.bucket(bucket_name)

    def put(self, analysis_id: str, archive: bytes) -> StoredArtifact:
        object_name = f"analyses/{analysis_id}/bundle.zip"
        blob = self._bucket.blob(object_name)
        try:
            blob.upload_from_string(
                archive,
                content_type="application/zip",
                if_generation_match=0,
                timeout=30,
            )
        except PreconditionFailed as exc:
            raise RollbackReadyError(
                "ARTIFACT_CONFLICT",
                "The analysis artifact already exists.",
                status_code=409,
                analysis_id=analysis_id,
            ) from exc
        if blob.generation is None:
            blob.reload(timeout=10)
        return StoredArtifact(
            object_name=object_name,
            generation=str(blob.generation),
            expires_at=datetime.now(UTC)
            + timedelta(hours=settings.rollbackready_artifact_retention_hours),
        )

    def get(self, object_name: str, generation: str) -> bytes:
        try:
            return self._bucket.blob(
                object_name,
                generation=int(generation),
            ).download_as_bytes(timeout=30)
        except NotFound as exc:
            raise _unavailable() from exc

    def delete(self, object_name: str, generation: str | None = None) -> None:
        try:
            self._bucket.blob(
                object_name,
                generation=int(generation) if generation else None,
            ).delete(timeout=30)
        except NotFound:
            return

    def check(self) -> None:
        if not self._bucket.exists(timeout=10):
            raise RuntimeError("RollbackReady artifact bucket is unavailable")


def build_artifact_store() -> ArtifactStore:
    if settings.rollbackready_artifact_bucket:
        return GcsArtifactStore(settings.rollbackready_artifact_bucket)
    return InMemoryArtifactStore()


def _unavailable() -> RollbackReadyError:
    return RollbackReadyError(
        "RAW_ARTIFACTS_DELETED",
        "The raw analysis artifacts are no longer available.",
        status_code=409,
    )
