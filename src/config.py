"""Shared Nebius configuration and project-relative storage paths."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
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
_CLIENT_CONTEXT = ContextVar("generation_client_context", default=None)


@dataclass(frozen=True)
class ClientSettings:
    api_key: str | None = field(repr=False)
    base_url: str


def capture_client_settings():
    return ClientSettings(get_api_key(), os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.us-central1.nebius.com/v1/"))


@contextmanager
def client_context(settings, check_cancel, remaining):
    token = _CLIENT_CONTEXT.set((settings, check_cancel, remaining))
    try:
        yield
    finally:
        _CLIENT_CONTEXT.reset(token)


def check_request():
    context = _CLIENT_CONTEXT.get()
    if context is not None:
        context[1]()


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
    context = _CLIENT_CONTEXT.get()
    if context is not None:
        return context[0].api_key
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
    context = _CLIENT_CONTEXT.get()
    options = {}
    if context is not None:
        check_request()
        options["timeout"] = max(0.1, min(30.0, context[2]()))
    client = OpenAI(
        api_key=key,
        base_url=context[0].base_url if context is not None else os.getenv(
            "NEBIUS_BASE_URL", "https://api.tokenfactory.us-central1.nebius.com/v1/"
        ),
        max_retries=0,
        **options,
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
