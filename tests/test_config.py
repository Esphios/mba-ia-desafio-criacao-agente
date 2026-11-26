from pathlib import Path

from aurora_assistant.config import Settings


def test_settings_use_project_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("AURORA_DATABASE_PATH", raising=False)

    settings = Settings(_env_file=None)

    assert settings.gemini_model == "gemini-3.8-flash"
    assert settings.database_path == tmp_path / ".runtime" / "aurora.db"
    assert settings.adk_database_path == tmp_path / ".runtime" / "aurora-adk.db"
    assert settings.data_dir == tmp_path / "dados"


def test_settings_accept_environment_overrides(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-custom")
    monkeypatch.setenv("AURORA_DATABASE_PATH", "state/test.db")

    settings = Settings(_env_file=None)

    assert settings.gemini_model == "gemini-custom"
    assert settings.database_path == tmp_path / "state" / "test.db"
    assert settings.adk_database_path == tmp_path / "state" / "test-adk.db"
