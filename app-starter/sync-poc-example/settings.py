"""Configuration. Reads .env, then real environment variables, then fails loudly.

Real environment variables WIN over .env, which is what you want: .env is for
your laptop, and a scheduler or container sets the real thing in production
without anyone having to delete a file.
"""

from __future__ import annotations

import sys

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Anything in .env that is not a field below is a typo, not a feature.
        extra="forbid",
    )

    src_url: str
    src_token_url: str
    src_client_id: str
    src_client_secret: SecretStr

    dst_url: str
    dst_token_url: str
    dst_client_id: str
    dst_client_secret: SecretStr

    source_path: str = "/ledger/entries"
    dest_path: str = "/postings"

    # Reads are idempotent and cheap to repeat, so they retry harder.
    source_max_attempts: int = 4
    source_max_elapsed: float = 20.0
    source_timeout: float = 15.0

    # POST is excluded from retries unless retry_non_idempotent is set. Do not
    # set it: a lost response would become two records.
    dest_max_attempts: int = 2
    dest_max_elapsed: float = 30.0
    dest_timeout: float = 30.0


def load() -> Settings:
    """Build Settings, or exit with a readable list of what is missing.

    Pydantic already reports every problem at once, which is what you want:
    fixing config one variable per run is miserable. It reports them inside a
    traceback though, so this trims it to the names and reasons.
    """
    try:
        return Settings()
    except ValidationError as exc:
        print("configuration is incomplete:\n", file=sys.stderr)
        for error in exc.errors():
            name = ".".join(str(part) for part in error["loc"]).upper()
            print(f"  {name}: {error['msg']}", file=sys.stderr)
        print("\nSet them in .env or the environment. See .env.example.",
              file=sys.stderr)
        raise SystemExit(3) from None


settings = load()
