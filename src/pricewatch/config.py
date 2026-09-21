"""Application configuration loaded from environment variables."""

from pathlib import Path

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated PriceWatch runtime settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PRICEWATCH_",
        extra="ignore",
    )

    data_dir: Path = Path("/data")
    database_url: str = "sqlite:////data/pricewatch.db"
    timezone: str = "Asia/Shanghai"
    app_secret_key: SecretStr = Field(min_length=32)
    external_url: AnyHttpUrl | None = None

