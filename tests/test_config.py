from app.core.config import Settings


def settings_with_database_url(database_url: str | None) -> Settings:
    return Settings(
        instance_connection_name=None,
        iam_database_user=None,
        database_name=None,
        database_url=database_url,
        cors_origins=(),
    )


def test_postgresql_url_uses_pg8000_driver() -> None:
    configured = settings_with_database_url(
        "postgresql://user:password@database:5432/app"
    )

    assert configured.sync_database_url == (
        "postgresql+pg8000://user:password@database:5432/app"
    )


def test_cloud_sql_mode_has_no_database_url() -> None:
    configured = settings_with_database_url(None)

    assert configured.sync_database_url is None
