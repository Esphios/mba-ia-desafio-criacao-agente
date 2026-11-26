from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração do serviço carregada da raiz do projeto ou do ambiente."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")
    gemini_model: str = Field(default="gemini-3.8-flash", alias="GEMINI_MODEL")
    database_path: Path = Field(
        default=Path(".runtime/aurora.db"), alias="AURORA_DATABASE_PATH"
    )
    data_dir: Path = Path("dados")

    @field_validator("database_path", "data_dir", mode="after")
    @classmethod
    def resolve_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else (Path.cwd() / value).resolve()

    @property
    def adk_database_path(self) -> Path:
        """Banco SQLite separado para sessões/eventos do ADK.

        Separar os escritores evita deadlock entre a transação de eventos do
        Runner e o ``BEGIN IMMEDIATE`` que garante exclusividade das reservas.
        """
        return self.database_path.with_name(
            f"{self.database_path.stem}-adk{self.database_path.suffix}"
        )

    @property
    def sqlalchemy_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.adk_database_path.as_posix()}"
