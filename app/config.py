from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    ENVIRONMENT: str = "local"
    DEBUG: bool = True

    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "aitax_webhooks_local"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""

    SYNTAGE_API_URL: str = "https://api.sandbox.syntage.com"
    SYNTAGE_API_KEY: str = ""
    SYNTAGE_WEBHOOK_SIGNING_SECRET: str = ""
    SYNTAGE_WEBHOOK_TOLERANCE: int = 300

    AITAX_INTERNAL_API_URL: str = "http://localhost:8000"
    AITAX_INTERNAL_API_TOKEN: str = ""
    AITAX_INTERNAL_API_TIMEOUT: int = 600

    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_TASK_ALWAYS_EAGER: bool = False

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )


settings = Settings()