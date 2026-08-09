from dotenv import load_dotenv

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


def test_process_environment_takes_precedence_over_dotenv(
    monkeypatch, tmp_path
) -> None:
    dotenv_path = tmp_path / ".env.local"
    dotenv_path.write_text("DATABASE_URL=postgresql://dotenv/app\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql://process/app")

    load_dotenv(dotenv_path, override=False)

    assert Settings.from_environment().database_url == "postgresql://process/app"


def test_clerk_and_vertex_settings_are_parsed(monkeypatch) -> None:
    monkeypatch.setenv("CLERK_SECRET_KEY", "test-secret")
    monkeypatch.setenv("CLERK_JWT_KEY", "test-jwt")
    monkeypatch.setenv(
        "CLERK_AUTHORIZED_PARTIES",
        "https://app.example.com, https://admin.example.com,",
    )
    monkeypatch.setenv("CLERK_AUTH_REQUIRED", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")

    configured = Settings.from_environment()

    assert configured.clerk_secret_key == "test-secret"
    assert configured.clerk_jwt_key == "test-jwt"
    assert configured.clerk_authorized_parties == (
        "https://app.example.com",
        "https://admin.example.com",
    )
    assert configured.clerk_auth_required is True
    assert configured.google_cloud_project == "test-project"
    assert configured.google_cloud_location == "global"
    assert configured.google_genai_use_vertexai is True
    assert configured.gemini_model == "test-model"
