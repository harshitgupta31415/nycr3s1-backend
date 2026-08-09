from app.core.database import Base
from app.models import AnalysisRecord, User


def test_user_model_is_registered_in_metadata() -> None:
    users = Base.metadata.tables["app.users"]

    assert users is User.__table__
    assert users.schema == "app"
    assert users.primary_key.columns.keys() == ["id"]
    assert users.c.clerk_user_id.unique is True
    assert users.c.clerk_user_id.index is True


def test_rollbackready_analysis_model_stores_sanitized_metadata() -> None:
    analyses = Base.metadata.tables["app.rollbackready_analyses"]

    assert analyses is AnalysisRecord.__table__
    assert "input_hash" in analyses.c
    assert analyses.c.owner_clerk_user_id.nullable is False
    assert analyses.c.owner_clerk_user_id.index is True
    assert "manifest" in analyses.c
    assert "evidence" in analyses.c
    assert "artifact_object_name" in analyses.c
    assert "active_operation_token" in analyses.c
    assert "row_version" in analyses.c
    assert "raw_archive" not in analyses.c
    assert "seed_sql" not in analyses.c
