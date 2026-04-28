"""
Configuración del microservicio.

Lee variables de entorno desde .env (en local) o del sistema (en prod).
Usar pydantic-settings garantiza que las variables tienen el tipo correcto.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Entorno ─────────────────────────────────────────
    ENVIRONMENT: str = "local"
    DEBUG: bool = True

    # ── Database del microservicio ──────────────────────
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "aitax_webhooks_local"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = "admin"

    # ── Syntage ─────────────────────────────────────────
    SYNTAGE_API_URL: str = "https://api.sandbox.syntage.com"
    SYNTAGE_API_KEY: str = ""
    SYNTAGE_WEBHOOK_SIGNING_SECRET: str = ""
    SYNTAGE_WEBHOOK_TOLERANCE: int = 300  # segundos

    # ── AITAX ───────────────────────────────────────────
    AITAX_INTERNAL_API_URL: str = "http://localhost:8000"
    AITAX_INTERNAL_API_TOKEN: str = ""

    # ── Redis / Celery ──────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    @property
    def database_url(self) -> str:
        """Construye URL de Postgres para SQLAlchemy."""
        return (
            f"postgresql+psycopg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

# Instancia global
settings = Settings()