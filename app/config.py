"""Application settings loaded from environment variables and .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WORKLOOP_",
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "WorkLoop"
    version: str = "0.1.0"
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False

    data_dir: Path = BASE_DIR / "data"
    db_filename: str = "workloop.db"
    key_filename: str = "secret.key"

    max_rounds: int = Field(default=6, ge=1)

    # Shell for Boss commands. "auto": pwsh, then Windows PowerShell, then cmd on Windows;
    # bash, then /bin/sh elsewhere.
    shell: Literal["auto", "pwsh", "powershell", "cmd", "bash", "sh"] = "auto"
    command_timeout_default: int = Field(default=120, ge=1)
    command_timeout_max: int = Field(default=600, ge=1)

    # Agent loop limits (per turn = one plan / test / task / fix step).
    boss_max_tool_calls_per_turn: int = Field(default=60, ge=1)
    worker_max_tool_calls_per_turn: int = Field(default=80, ge=1)
    max_llm_calls_per_turn: int = Field(default=100, ge=1)
    # Shorten old tool results when the history passes this share of the model's context window.
    context_threshold: float = Field(default=0.75, gt=0, le=1)
    default_context_window: int = Field(default=128_000, ge=1000)

    # WebSocket heartbeat: server pings every interval, closes if nothing comes back in time.
    ws_ping_interval: float = Field(default=30, gt=0)
    ws_pong_timeout: float = Field(default=10, gt=0)

    # Electron pages loaded from file:// send "Origin: null". Accept it for CORS and the WebSocket.
    allow_null_origin: bool = False

    @field_validator("data_dir")
    @classmethod
    def _resolve_data_dir(cls, value: Path) -> Path:
        # Relative paths are relative to the project root, not the current directory.
        return value if value.is_absolute() else (BASE_DIR / value).resolve()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @computed_field  # type: ignore[prop-decorator]
    @property
    def key_file_path(self) -> Path:
        return self.data_dir / self.key_filename

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
