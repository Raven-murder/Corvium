"""Shared Nebius configuration and project-relative storage paths."""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

APP_NAME = "Corvium"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
EMBED_MODEL = "Qwen/Qwen3-Embedding-8B"
CHAT_MODEL = os.getenv("NEBIUS_MODEL", "nvidia/nemotron-3-super-120b-a12b")
DATA_DIR = PROJECT_ROOT / "data"


def _limit(name, default):
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


WORKSPACE_CHAT_LIMIT = _limit("WORKSPACE_CHAT_LIMIT", 60)
WORKSPACE_TOOL_LIMIT = _limit("WORKSPACE_TOOL_LIMIT", 15)
WORKSPACE_UPLOAD_LIMIT = _limit("WORKSPACE_UPLOAD_LIMIT", 20)
GLOBAL_API_ACTION_LIMIT = _limit("GLOBAL_API_ACTION_LIMIT", 3000)


def get_api_key():
    key = os.environ.get("NEBIUS_API_KEY")
    if key:
        return key
    try:
        return st.secrets.get("NEBIUS_API_KEY")
    except (StreamlitSecretNotFoundError, FileNotFoundError):
        return None


def get_vision_model():
    return os.getenv("NEBIUS_VISION_MODEL", "").strip()


def get_client():
    key = get_api_key()
    if not key:
        raise RuntimeError("NEBIUS_API_KEY is missing from the environment and Streamlit secrets.")
    client = OpenAI(
        api_key=key,
        base_url=os.getenv(
            "NEBIUS_BASE_URL", "https://api.tokenfactory.us-central1.nebius.com/v1/"
        ),
        max_retries=0,
    )
    if __package__:
        from .usage import guard_api_call
    else:
        from usage import guard_api_call
    client.chat.completions.create = guard_api_call(client.chat.completions.create)
    client.embeddings.create = guard_api_call(client.embeddings.create)
    client.models.list = guard_api_call(client.models.list)
    return client


def safe_error(error):
    if getattr(error, "public_message", False):
        return str(error)
    message = str(error)
    key = get_api_key()
    if key:
        message = message.replace(key, "[REDACTED]")
    return f"{type(error).__name__}: {message}"
