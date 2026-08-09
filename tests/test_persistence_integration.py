from __future__ import annotations

import os
from threading import Event, Thread
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from app.rollbackready.artifacts import InMemoryArtifactStore
from app.rollbackready.errors import RollbackReadyError
from app.rollbackready.intake import build_demo_archive, load_demo_bundle
from app.rollbackready.persistence import SqlAlchemyEvidenceRepository
from app.rollbackready.service import AnalysisService
from tests.test_rollbackready import _FakePlanner, _FakeSimulator


@pytest.fixture
def postgres_repository():
    database_url = os.getenv("ROLLBACKREADY_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ROLLBACKREADY_TEST_DATABASE_URL is not configured")
    engine = create_engine(database_url)
    repository = SqlAlchemyEvidenceRepository(lambda: engine)
    try:
        yield repository
    finally:
        engine.dispose()


def _service(
    repository: SqlAlchemyEvidenceRepository,
    artifact_store: InMemoryArtifactStore,
    *,
    simulator=None,
) -> AnalysisService:
    return AnalysisService(
        simulator=simulator or _FakeSimulator(),
        planner=_FakePlanner(),
        repository=repository,
        artifact_store=artifact_store,
    )


def test_two_services_share_state_idempotency_and_quotas(
    postgres_repository: SqlAlchemyEvidenceRepository,
) -> None:
    owner = f"integration-{uuid4()}"
    artifact_store = InMemoryArtifactStore()
    first = _service(postgres_repository, artifact_store)
    second = _service(postgres_repository, artifact_store)
    staged = first.create(
        load_demo_bundle(), owner, build_demo_archive()
    )
    try:
        assert second.get(staged.id, owner).id == staged.id
        assert second.run(staged.id, owner).raw_artifacts_available
        plan = first.create_plan(staged.id, owner)
        assert second.verify_plan(staged.id, plan.id, owner).status == "PASS"
        assert not first.get(staged.id, owner).raw_artifacts_available

        state, _, key_hash = first.begin_idempotency(owner, "probe", "same-key")
        assert state == "NEW"
        assert second.begin_idempotency(owner, "probe", "same-key")[0] == "IN_PROGRESS"
        first.finish_idempotency(owner, "probe", key_hash, staged.id, {"id": staged.id})
        assert second.begin_idempotency(owner, "probe", "same-key")[0] == "COMPLETE"

        first.check_rate_limit(owner, "integration", 1, 60)
        with pytest.raises(RollbackReadyError) as rate_error:
            second.check_rate_limit(owner, "integration", 1, 60)
        assert rate_error.value.code == "RATE_LIMITED"
    finally:
        postgres_repository.delete(staged.id, owner)


class _BlockingSimulator(_FakeSimulator):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def run(self, *args: object):
        self.started.set()
        assert self.release.wait(timeout=10)
        return super().run(*args)


def test_delete_during_run_cannot_resurrect_analysis(
    postgres_repository: SqlAlchemyEvidenceRepository,
) -> None:
    owner = f"delete-race-{uuid4()}"
    artifact_store = InMemoryArtifactStore()
    simulator = _BlockingSimulator()
    runner = _service(
        postgres_repository,
        artifact_store,
        simulator=simulator,
    )
    deleter = _service(postgres_repository, artifact_store)
    staged = runner.create(load_demo_bundle(), owner, build_demo_archive())
    failures: list[BaseException] = []

    def run_analysis() -> None:
        try:
            runner.run(staged.id, owner)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            failures.append(exc)

    thread = Thread(target=run_analysis)
    thread.start()
    assert simulator.started.wait(timeout=10)
    deleter.delete(staged.id, owner)
    simulator.release.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], RollbackReadyError)
    assert failures[0].code == "ANALYSIS_NOT_FOUND"
    assert postgres_repository.get(staged.id, owner) is None


def test_unfinished_analysis_limit_is_shared_across_services(
    postgres_repository: SqlAlchemyEvidenceRepository,
) -> None:
    owner = f"capacity-{uuid4()}"
    artifact_store = InMemoryArtifactStore()
    staged_ids: list[str] = []
    try:
        for _ in range(5):
            service = _service(postgres_repository, artifact_store)
            staged_ids.append(
                service.create(
                    load_demo_bundle(), owner, build_demo_archive()
                ).id
            )
        with pytest.raises(RollbackReadyError) as capacity_error:
            _service(postgres_repository, artifact_store).create(
                load_demo_bundle(), owner, build_demo_archive()
            )
        assert capacity_error.value.code == "ANALYSIS_CAPACITY_REACHED"
    finally:
        for analysis_id in staged_ids:
            postgres_repository.delete(analysis_id, owner)
