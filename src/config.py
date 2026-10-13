"""Shared Nebius configuration and project-relative storage paths."""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
EMBED_MODEL = "Qwen/Qwen3-Embedding-8B"
CHAT_MODEL = os.getenv("NEBIUS_MODEL", "nvidia/nemotron-3-super-120b-a12b")
DB_PATH = PROJECT_ROOT / "data" / "app.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_client():
    key = os.environ.get("NEBIUS_API_KEY")
    if not key:
        raise RuntimeError("NEBIUS_API_KEY is missing from the environment.")
    return OpenAI(
        api_key=key,
        base_url=os.getenv(
            "NEBIUS_BASE_URL", "https://api.tokenfactory.us-central1.nebius.com/v1/"
        ),
        max_retries=0,
    )


def safe_error(error):
    message = str(error)
    key = os.environ.get("NEBIUS_API_KEY")
    if key:
        message = message.replace(key, "[REDACTED]")
    return f"{type(error).__name__}: {message}"
