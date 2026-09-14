"""Configuration for this application."""

from __future__ import annotations

import sys

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLS_", env_file=".env", extra="forbid"
    )

    base_url: str
    auth_url: str
    client_id: str
    client_secret: SecretStr


def load() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        print("configuration is incomplete:\n", file=sys.stderr)
        for error in exc.errors():
            print(f"  CLS_{error['loc'][0].upper()}: {error['msg']}", file=sys.stderr)
        raise SystemExit(3) from None


settings = load()
