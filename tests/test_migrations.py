import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = BACKEND_ROOT / "alembic" / "versions" / "0002_create_users.py"
OWNERSHIP_MIGRATION_PATH = (
    BACKEND_ROOT / "alembic" / "versions" / "0004_add_analysis_ownership.py"
)
STATEMENT_EVIDENCE_MIGRATION_PATH = (
    BACKEND_ROOT / "alembic" / "versions" / "0005_add_statement_evidence.py"
)


def load_users_migration():
    spec = importlib.util.spec_from_file_location("users_migration", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_ownership_migration():
    spec = importlib.util.spec_from_file_location(
        "ownership_migration", OWNERSHIP_MIGRATION_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_statement_evidence_migration():
    spec = importlib.util.spec_from_file_location(
        "statement_evidence_migration", STATEMENT_EVIDENCE_MIGRATION_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rollbackready_revision_is_the_alembic_head() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    revision = scripts.get_revision("0002_create_users")

    ownership_revision = scripts.get_revision("0004_add_analysis_ownership")
    statement_revision = scripts.get_revision("0005_add_statement_evidence")

    assert scripts.get_current_head() == "0005_add_statement_evidence"
    assert revision is not None
    assert revision.down_revision == "0001_create_app_schema"
    assert ownership_revision is not None
    assert ownership_revision.down_revision == "0003_create_rollbackready_evidence"
    assert statement_revision is not None
    assert statement_revision.down_revision == "0004_add_analysis_ownership"


def test_users_revision_upgrade_and_downgrade_structure(monkeypatch) -> None:
    migration = load_users_migration()
    calls: list[tuple[str, tuple, dict]] = []

    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda *args, **kwargs: calls.append(("create_table", args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda *args, **kwargs: calls.append(("create_index", args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda *args, **kwargs: calls.append(("drop_index", args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_table",
        lambda *args, **kwargs: calls.append(("drop_table", args, kwargs)),
    )

    migration.upgrade()
    migration.downgrade()

    assert [call[0] for call in calls] == [
        "create_table",
        "create_index",
        "drop_index",
        "drop_table",
    ]
    assert calls[0][1][0] == "users"
    assert calls[0][2]["schema"] == "app"
    assert calls[1][1][0] == "ix_app_users_clerk_user_id"
    assert calls[2][1][0] == "ix_app_users_clerk_user_id"
    assert calls[3][1][0] == "users"


def test_ownership_revision_upgrade_and_downgrade_structure(monkeypatch) -> None:
    migration = load_ownership_migration()
    calls: list[tuple[str, tuple, dict]] = []

    for operation in (
        "add_column",
        "alter_column",
        "create_index",
        "drop_index",
        "drop_column",
    ):
        monkeypatch.setattr(
            migration.op,
            operation,
            lambda *args, _operation=operation, **kwargs: calls.append(
                (_operation, args, kwargs)
            ),
        )

    migration.upgrade()
    migration.downgrade()

    assert [call[0] for call in calls] == [
        "add_column",
        "alter_column",
        "create_index",
        "drop_index",
        "drop_column",
    ]
    added_column = calls[0][1][1]
    assert calls[0][1][0] == "rollbackready_analyses"
    assert calls[0][2]["schema"] == "app"
    assert added_column.name == "owner_clerk_user_id"
    assert added_column.nullable is False
    assert calls[1][2]["server_default"] is None
    assert calls[2][1][0] == (
        "ix_app_rollbackready_analyses_owner_clerk_user_id"
    )
    assert calls[3][1][0] == (
        "ix_app_rollbackready_analyses_owner_clerk_user_id"
    )
    assert calls[4][1][1] == "owner_clerk_user_id"


def test_statement_evidence_revision_upgrade_and_downgrade_structure(
    monkeypatch,
) -> None:
    migration = load_statement_evidence_migration()
    calls: list[tuple[str, tuple, dict]] = []
    for operation in ("add_column", "alter_column", "drop_column"):
        monkeypatch.setattr(
            migration.op,
            operation,
            lambda *args, _operation=operation, **kwargs: calls.append(
                (_operation, args, kwargs)
            ),
        )

    migration.upgrade()
    migration.downgrade()

    assert [call[0] for call in calls] == [
        "add_column",
        "alter_column",
        "drop_column",
    ]
    added_column = calls[0][1][1]
    assert added_column.name == "statements"
    assert added_column.nullable is False
    assert calls[1][2]["server_default"] is None
    assert calls[2][1][1] == "statements"
