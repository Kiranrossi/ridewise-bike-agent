"""Application settings, read from environment variables (.env)."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


@dataclass(frozen=True)
class Settings:
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "")
    llm_timeout: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    llm_reasoning_effort: str = os.getenv("LLM_REASONING_EFFORT", "medium")
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.2"))
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    db_path: Path = _path(os.getenv("DB_PATH", "Data/ridewise.db"))
    index_path: Path = _path(os.getenv("INDEX_PATH", "index"))
    rules_dir: Path = ROOT / "rules"
    auth_mode: str = os.getenv("AUTH_MODE", "strict")


settings = Settings()
