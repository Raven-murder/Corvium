"""Shared Nebius configuration and project-relative storage paths."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import os
import re
from threading import RLock
from urllib.parse import unquote, urlsplit
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
    database_url: str | None = field(default=None, repr=False)


def capture_client_settings():
    return ClientSettings(get_api_key(), os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.us-central1.nebius.com/v1/"), get_database_url())


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

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_WORKSPACE_BYTES = _limit("WORKSPACE_STORAGE_LIMIT_MB", 80) * 1024 * 1024
DB_SOFT_LIMIT_MB = _limit("DB_SOFT_LIMIT_MB", 400)
WORKSPACE_RETENTION_DAYS = _limit("WORKSPACE_RETENTION_DAYS", 30)
_DATABASE_SECRET_LOCK = RLock()
_DATABASE_SECRET_READY = False
_DATABASE_SECRET = None


class StorageError(RuntimeError):
    public_message = True

    def __init__(self, message="Storage is unavailable, please try again"):
        super().__init__(message)


def get_database_url():
    global _DATABASE_SECRET_READY, _DATABASE_SECRET
    value = os.environ.get("DATABASE_URL")
    if value and value.strip():
        return value.strip()
    context = _CLIENT_CONTEXT.get()
    if context is not None:
        return context[0].database_url
    with _DATABASE_SECRET_LOCK:
        if not _DATABASE_SECRET_READY:
            try:
                value = st.secrets.get("DATABASE_URL")
                _DATABASE_SECRET = value.strip() if isinstance(value, str) and value.strip() else None
            except (StreamlitSecretNotFoundError, FileNotFoundError):
                _DATABASE_SECRET = None
            except Exception:
                raise StorageError() from None
            _DATABASE_SECRET_READY = True
        return _DATABASE_SECRET


def redact_storage_secrets(message):
    text = re.sub(r"postgres(?:ql)?://[^\s\"'<>]+", "[REDACTED]", str(message), flags=re.I)
    try:
        url = get_database_url()
        if url:
            text = text.replace(url, "[REDACTED]")
            password = urlsplit(url).password
            if password:
                text = text.replace(password, "[REDACTED]").replace(unquote(password), "[REDACTED]")
            for match in re.finditer(r"password\s*=\s*(?:'([^']*)'|([^\s]+))", url, re.I):
                text = text.replace(match.group(1) or match.group(2), "[REDACTED]")
    except Exception:
        pass
    return text


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
    if isinstance(error, StorageError):
        return redact_storage_secrets(str(error))
    if error.__class__.__module__.startswith(("psycopg", "sqlite3")):
        return "Storage is unavailable, please try again"
    message = redact_storage_secrets(str(error))
    key = get_api_key()
    if key:
        message = message.replace(key, "[REDACTED]")
    return message if getattr(error, "public_message", False) else f"{type(error).__name__}: {message}"


if __package__:
    from . import perf
else:
    import perf

if perf.ENABLED:
    OpenAI = perf.client_factory(OpenAI)
