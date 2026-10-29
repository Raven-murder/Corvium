"""Seed disposable demo storage using the existing ingestion and memory APIs."""

from threading import RLock

from .config import PROJECT_ROOT
from .db import connect, delete_document
from .ingest import ingest_files
from .memory import delete_memory, list_memories, save_memories

_LOCK = RLock()
_SAMPLE_MEMORIES = [
    {"text": "DBMS exam is on 5 Nov", "kind": "deadline"},
    {"text": "Weak at normalization", "kind": "weak_topic"},
]


def _seed_demo_files():
    """Seed only an empty document library; preserve all existing records."""
    with _LOCK:
        connection = connect()
        try:
            if connection.execute("SELECT 1 FROM documents LIMIT 1").fetchone():
                return
        finally:
            connection.close()
        paths = sorted(
            path for path in (PROJECT_ROOT / "demo_data").glob("*")
            if path.is_file() and path.suffix.lower() in {".md", ".docx", ".pptx", ".png"}
        )
        if not paths:
            raise RuntimeError("No demo notes were found in demo_data/.")
        original_memories = {item["id"] for item in list_memories()}
        added_documents = []
        try:
            save_memories(_SAMPLE_MEMORIES)
            results = ingest_files(paths)
            added_documents.extend(result["name"] for result in results if not result["error"])
        except Exception:
            for name in added_documents:
                delete_document(name)
            for memory in list_memories():
                if memory["id"] not in original_memories:
                    delete_memory(memory["id"])
            raise
        return results



def seed_demo():
    from .workspaces import get_storage, ensure_workspace
    ensure_workspace(get_storage())
    return []


def reset_demo():
    from .workspaces import restore_workspace
    restore_workspace()
    return []
