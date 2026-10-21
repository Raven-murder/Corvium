"""Seed disposable demo storage using the existing ingestion and memory APIs."""

from threading import RLock

from .config import PROJECT_ROOT
from .db import connect, delete_document
from .ingest import ingest_file
from .memory import delete_memory, list_memories, save_memories

_LOCK = RLock()
_SAMPLE_MEMORIES = [
    {"text": "DBMS exam is on 5 Nov", "kind": "deadline"},
    {"text": "Weak at normalization", "kind": "weak_topic"},
]


def seed_demo():
    """Seed only an empty document library; preserve all existing records."""
    with _LOCK:
        connection = connect()
        try:
            if connection.execute("SELECT 1 FROM documents LIMIT 1").fetchone():
                return
        finally:
            connection.close()
        paths = sorted((PROJECT_ROOT / "demo_data").glob("*.md"))
        if not paths:
            raise RuntimeError("No demo notes were found in demo_data/.")
        original_memories = {item["id"] for item in list_memories()}
        added_documents = []
        try:
            save_memories(_SAMPLE_MEMORIES)
            for path in paths:
                ingest_file(path)
                added_documents.append(path.name)
        except Exception:
            # Keep an unsuccessful seed retryable on the next startup.
            for name in added_documents:
                delete_document(name)
            for memory in list_memories():
                if memory["id"] not in original_memories:
                    delete_memory(memory["id"])
            raise


def reset_demo():
    """Clear application tables and restore the bundled demo."""
    with _LOCK:
        connection = connect()
        try:
            with connection:
                connection.execute("DELETE FROM chunks")
                connection.execute("DELETE FROM documents")
                connection.execute("DELETE FROM memories")
                connection.execute("DELETE FROM messages")
        finally:
            connection.close()
        seed_demo()
