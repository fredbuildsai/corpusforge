"""Runtime settings and config-file loading.

`Settings` is the single place paths and identity live. Two ways to configure it:

- standalone: `CF_*` environment variables / `.env` in the project directory (the current working directory);
- embedded: a host application that has its own settings class (e.g. one with a different env prefix) builds a
  `Settings` (or subclass) and installs it with `set_settings()`; every corpusforge module then follows it.

`PROJECT_ROOT` is the current working directory (like git/npm/cargo): `cd` into any project folder and
`configs/`, `data/` and `.env` resolve there. Package assets (templates, migrations) ship inside the package
and are located relative to the package, never to the project.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
PROJECT_ROOT = Path.cwd()

_override: "Settings | None" = None


def load_env(env_file: Path | None = None) -> None:
    """Export `.env` into `os.environ` so LiteLLM and the router see provider keys.

    pydantic-settings only reads prefixed fields into Settings; provider keys such as `OPENROUTER_API_KEY` must
    be real environment variables. Variables already set in the shell win.
    """
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CF_", env_file=PROJECT_ROOT / ".env", extra="ignore")

    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'corpus.db'}"
    log_database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'logs.db'}"
    data_dir: Path = PROJECT_ROOT / "data"
    configs_dir: Path = PROJECT_ROOT / "configs"
    contact_email: str = ""
    # Identity sent in the User-Agent of every polite HTTP request (OpenAlex/Crossref ask for a contact).
    app_name: str = "corpusforge"
    app_url: str = "https://github.com/fredbuildsai/corpusforge"
    # Tokenizer used to count chunk sizes (a HuggingFace tokenizer id). Match your target model's tokenizer.
    tokenizer_model: str = "unsloth/gemma-4-E2B-it"
    allow_paid: bool = False
    max_usd_per_day: float = 5.0

    @field_validator("database_url", "log_database_url")
    @classmethod
    def _anchor_sqlite_path(cls, url: str) -> str:
        """Resolve a relative SQLite path against the project root, not the shell's working directory."""
        prefix = "sqlite:///"
        if url.startswith(prefix) and not Path(url.removeprefix(prefix)).is_absolute():
            return f"{prefix}{PROJECT_ROOT / url.removeprefix(prefix)}"
        return url

    @field_validator("data_dir", "configs_dir")
    @classmethod
    def _anchor_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "export"


def set_settings(settings: Settings | None) -> None:
    """Install the settings every corpusforge module should use (None restores the standalone default)."""
    global _override
    _override = settings
    _default_settings.cache_clear()


@lru_cache
def _default_settings() -> Settings:
    load_env()
    return Settings()


def get_settings() -> Settings:
    return _override if _override is not None else _default_settings()


def load_config(name: str, settings: Settings | None = None) -> dict[str, Any]:
    """Load `<configs_dir>/<name>.yaml`."""
    settings = settings or get_settings()
    with open(settings.configs_dir / f"{name}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)
