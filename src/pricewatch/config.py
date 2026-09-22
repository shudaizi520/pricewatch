"""Application configuration loaded from environment variables."""

from pathlib import Path
from typing import Self

from pydantic import AnyHttpUrl, Field, IPvAnyAddress, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated PriceWatch runtime settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PRICEWATCH_",
        env_ignore_empty=True,
        extra="ignore",
    )

    data_dir: Path = Path("/data")
    database_url: str = "sqlite:////data/pricewatch.db"
    timezone: str = "Asia/Shanghai"
    app_secret_key: SecretStr = Field(min_length=32)
    external_url: AnyHttpUrl | None = None
    dell_socks_proxy_host: IPvAnyAddress | None = None
    dell_socks_proxy_port: int | None = Field(default=None, ge=1, le=65535)

    @model_validator(mode="after")
    def require_complete_dell_proxy(self) -> Self:
        if (self.dell_socks_proxy_host is None) != (self.dell_socks_proxy_port is None):
            raise ValueError("Dell SOCKS proxy host and port must be configured together")
        return self
