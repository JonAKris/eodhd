"""
config.py
---------
Settings loaded from environment variables (and an optional .env file).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Tiny .env loader so we don't force a python-dotenv dependency.
ENV_PATH = Path(__file__).resolve().parent / ".env"
if ENV_PATH.exists():
    for raw in ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    eodhd_api_key: str = os.getenv("EODHD_API_KEY", "demo")

    db_host: str = os.getenv("PG_HOST", "localhost")
    db_port: int = int(os.getenv("PG_PORT", "5432"))
    db_name: str = os.getenv("PG_DB", "eodhd")
    db_user: str = os.getenv("PG_USER", "postgres")
    db_password: str = os.getenv("PG_PASSWORD", "postgres")
    db_pool_max: int = int(os.getenv("PG_POOL_MAX", "10"))

    # Read-only role (agent, explorer, dashboard). Falls back to the writer
    # identity if unset, so single-role setups keep working.
    db_ro_user: str = os.getenv("PG_RO_USER", os.getenv("PG_USER", "postgres"))
    db_ro_password: str = os.getenv("PG_RO_PASSWORD", os.getenv("PG_PASSWORD", "postgres"))

    dash_host: str = os.getenv("DASH_HOST", "127.0.0.1")
    dash_port: int = int(os.getenv("DASH_PORT", "8050"))
    dash_debug: bool = os.getenv("DASH_DEBUG", "true").lower() == "true"

    @property
    def dsn(self) -> str:
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={self.db_user} password={self.db_password}"
        )

    @property
    def ro_dsn(self) -> str:
        """Read-only connection string (agent / explorer / dashboard). Uses the
        read-only role, falling back to the writer identity when PG_RO_* is
        unset."""
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={self.db_ro_user} password={self.db_ro_password}"
        )


settings = Settings()