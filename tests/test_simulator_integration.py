from __future__ import annotations

import io
import os
import shutil
import subprocess
import zipfile

import pytest

from app.rollbackready.artifacts import InMemoryArtifactStore
from app.rollbackready.errors import RollbackReadyError
from app.rollbackready.intake import (
    build_demo_archive,
    load_demo_bundle,
    load_project_bundle,
)
from app.rollbackready.persistence import NullEvidenceRepository
from app.rollbackready.service import AnalysisService


def _require_docker() -> None:
    if os.getenv("ROLLBACKREADY_REQUIRE_DOCKER_TESTS") != "true":
        pytest.skip("Docker simulator integration is not required in this run")
    if not shutil.which("docker"):
        pytest.fail("Docker is required for the simulator integration gate")
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if probe.returncode:
        pytest.fail("Docker daemon is unavailable for the simulator integration gate")


def test_postgresql_18_exact_demo_lifecycle_and_cleanup() -> None:
    _require_docker()
    artifact_store = InMemoryArtifactStore()
    service = AnalysisService(
        repository=NullEvidenceRepository(),
        artifact_store=artifact_store,
    )
    archive = build_demo_archive()
    staged = service.create(load_demo_bundle(), "docker-user", archive)
    object_name = f"analyses/{staged.id}/bundle.zip"

    analyzed = service.run(staged.id, "docker-user")
    assert analyzed.verdict == "UNSAFE"
    assert analyzed.raw_artifacts_available

    plan = service.create_plan(staged.id, "docker-user")
    assert plan.provider in {"gemini", "deterministic-fallback"}
    verification = service.verify_plan(staged.id, plan.id, "docker-user")
    assert verification.status == "PASS"
    assert verification.verdict == "VERIFIED_FOR_REVIEW"
    assert service.report(staged.id, "docker-user").analysis.id == staged.id
    assert not service.get(staged.id, "docker-user").raw_artifacts_available

    with pytest.raises(RollbackReadyError):
        artifact_store.get(object_name, "1")
    service.delete(staged.id, "docker-user")
    with pytest.raises(RollbackReadyError) as deleted:
        service.get(staged.id, "docker-user")
    assert deleted.value.code == "ANALYSIS_NOT_FOUND"

    leaked = subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"name=rr-{staged.id[:8]}-",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert not leaked.stdout.strip()


def test_postgresql_18_safe_idempotent_candidate_proves_recovery() -> None:
    _require_docker()
    source = zipfile.ZipFile(io.BytesIO(build_demo_archive()))
    buffer = io.BytesIO()
    candidate_path = (
        "prisma/migrations/20260809100000_add_phone/migration.sql"
    )
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == candidate_path:
                content = b"ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;"
            archive.writestr(info.filename, content)
    bundle = load_project_bundle(
        buffer.getvalue(),
        "20260809100000_add_phone",
    )

    service = AnalysisService(repository=NullEvidenceRepository())
    staged = service.create(bundle, "safe-docker-user")
    result = service.run(staged.id, "safe-docker-user")

    assert staged.status == "STAGED"
    assert result.verdict == "VERIFIED_FOR_REVIEW"
    assert all(
        dimension.status == "PASS" for dimension in result.evidence
    )
    service.delete(staged.id, "safe-docker-user")
