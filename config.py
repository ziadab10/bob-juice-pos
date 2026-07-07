"""Application configuration for BOB JUICE POS."""

import os
import secrets
from decimal import Decimal
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bob_juice.db"
FRESH_DB_MARKER = BASE_DIR / ".fresh_db"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
SECRET_KEY_FILE = BASE_DIR / ".secret_key"

# Default global modifiers (overridden by system_settings table at runtime)
DEFAULT_EXCHANGE_RATE_USD_LBP = Decimal("89500.00")
DEFAULT_TOTERS_COMMISSION_PCT = Decimal("22.00")
DEFAULT_TALABNA_COMMISSION_PCT = Decimal("15.00")
DEFAULT_MARKIT_COMMISSION_PCT = Decimal("18.00")

BRAND_LOGO_PATH = STATIC_DIR / "logo.png"
BRAND_WORDMARK_PATH = STATIC_DIR / "brand-logo.png"
CIRCULAR_LOGO_PATH = STATIC_DIR / "logo_circular.png"
LOGO_PATH = CIRCULAR_LOGO_PATH


def load_persistent_secret_key() -> str:
    """Stable JWT secret across restarts — env var, .env, or .secret_key file."""
    env_key = os.environ.get("SECRET_KEY", "").strip()
    if env_key:
        return env_key
    if SECRET_KEY_FILE.exists():
        stored = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    generated = secrets.token_urlsafe(48)
    SECRET_KEY_FILE.write_text(generated, encoding="utf-8")
    return generated


class Settings(BaseSettings):
    app_name: str = "BOB JUICE POS"
    secret_key: str = Field(default_factory=load_persistent_secret_key)
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 480
    database_url: str = f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"
    # Auth cookies (HttpOnly — survives refresh; paired with localStorage token)
    session_cookie_name: str = "bob_pos_token"
    admin_session_cookie_name: str = "bob_admin_token"
    cookie_secure: bool = Field(default=False, validation_alias="COOKIE_SECURE")
    # Multi-branch hybrid sync
    branch_id: int = 1
    branch_code: str = "MAIN"
    is_central_server: bool = True
    central_sync_url: str = ""
    sync_api_key: str = ""
    sync_interval_seconds: int = 60
    # Direct thermal receipt printing (Windows)
    thermal_printer_name: str = ""
    thermal_print_enabled: bool = True
    reset_db_on_startup: bool = Field(default=False, validation_alias="BOB_RESET_DB")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
