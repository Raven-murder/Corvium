"""SQLite storage; vectors are serialized as float32 bytes."""

from contextlib import contextmanager
import sqlite3
from pathlib import Path

import numpy as np

if __package__:
    from .workspaces import get_storage, ensure_workspace, bound_database_identity, database_identity, WorkspaceError
else:
    from workspaces import get_storage, ensure_workspace, bound_database_identity, database_identity, WorkspaceError


def connect():
    storage = get_storage()
    identity = bound_database_identity()
    if identity is not None:
        if database_identity(storage) != identity:
            raise WorkspaceError("This workspace changed while the answer was running.")
        connection = sqlite3.connect(storage.db_path.as_uri() + "?mode=rw", timeout=30, uri=True)
        try:
            if database_identity(storage) != identity:
                raise WorkspaceError("This workspace changed while the answer was running.")
        except BaseException:
            connection.close()
            raise
    else:
        ensure_workspace(storage)
        connection = sqlite3.connect(storage.db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def embedding_to_blob(vector):
    return np.asarray(vector, dtype=np.float32).tobytes()


def embedding_from_blob(blob):
    return np.frombuffer(blob, dtype=np.float32)


_EXTENSIONS = {
    "pdf": (".pdf",),
    "docx": (".docx",),
    "pptx": (".pptx",),
    "html": (".html", ".htm"),
    "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".avif", ".ico"),
}


def infer_document_kind(name):
    suffix = Path(name).suffix.lower()
    return next((kind for kind, extensions in _EXTENSIONS.items() if suffix in extensions), "text")


def _add_column(connection, table, name, definition):
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if name in columns:
        return False
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    return True


def _migrate(connection):
    """Extend legacy tables and backfill only missing metadata in one transaction."""
    connection.execute("""CREATE TABLE IF NOT EXISTS folders (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, position INTEGER NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        body_json TEXT NOT NULL, days INTEGER NOT NULL,
        created_at TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL,
        doc_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS bookmarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        locator TEXT NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS notes_doc_id ON notes(doc_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS bookmarks_doc_id ON bookmarks(doc_id)")
    for name, definition in (
        ("folder_id", "INTEGER REFERENCES folders(id) ON DELETE SET NULL"),
        ("position", "INTEGER"), ("display_name", "TEXT"),
        ("kind", "TEXT"), ("stored_path", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'indexed'"),
    ):
        _add_column(connection, "documents", name, definition)
    _add_column(connection, "chunks", "section", "TEXT")
    _add_column(connection, "messages", "chat_id", "INTEGER REFERENCES chats(id) ON DELETE CASCADE")
    _add_column(connection, "messages", "sources", "TEXT NOT NULL DEFAULT '[]'")
    _add_column(connection, "messages", "error", "INTEGER NOT NULL DEFAULT 0")
    _add_column(connection, "messages", "kind", "TEXT NOT NULL DEFAULT 'text'")
    connection.execute("UPDATE messages SET kind = 'text' WHERE kind IS NULL OR kind = ''")
    for table in ("chats", "messages"):
        _add_column(connection, table, "pinned", "INTEGER NOT NULL DEFAULT 0")
        _add_column(connection, table, "pinned_at", "TEXT")
        connection.execute(f"UPDATE {table} SET pinned = 0 WHERE pinned IS NULL")
        connection.execute(f"UPDATE {table} SET pinned_at = created_at WHERE pinned != 0 AND pinned_at IS NULL")
    connection.execute("UPDATE documents SET display_name = name WHERE display_name IS NULL")
    rows = connection.execute("SELECT id, name FROM documents WHERE kind IS NULL").fetchall()
    connection.executemany("UPDATE documents SET kind = ? WHERE id = ?", [
        (infer_document_kind(row["name"]), row["id"]) for row in rows
    ])
    rows = connection.execute(
        "SELECT id, folder_id FROM documents WHERE position IS NULL ORDER BY name COLLATE NOCASE, id"
    ).fetchall()
    for row in rows:
        position = connection.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM documents WHERE folder_id IS ?",
            (row["folder_id"],),
        ).fetchone()[0]
        connection.execute("UPDATE documents SET position = ? WHERE id = ?", (position, row["id"]))
    legacy = connection.execute(
        "SELECT MIN(created_at), MAX(created_at) FROM messages WHERE chat_id IS NULL"
    ).fetchone()
    if legacy[0] is not None:
        cursor = connection.execute(
            "INSERT INTO chats(title, created_at, updated_at) VALUES ('Earlier chat', ?, ?)",
            (legacy[0], legacy[1]),
        )
        connection.execute("UPDATE messages SET chat_id = ? WHERE chat_id IS NULL", (cursor.lastrowid,))
    kind_cases = " ".join(
        f"WHEN lower(NEW.name) LIKE '%{extension}' THEN '{kind}'"
        for kind, extensions in _EXTENSIONS.items() for extension in extensions
    )
    connection.execute(f"""CREATE TRIGGER IF NOT EXISTS documents_defaults
        AFTER INSERT ON documents
        BEGIN
            UPDATE documents SET
                display_name = COALESCE(NEW.display_name, NEW.name),
                kind = COALESCE(NEW.kind, CASE {kind_cases} ELSE 'text' END),
                position = COALESCE(NEW.position, (
                    SELECT COALESCE(MAX(position), -1) + 1 FROM documents
                    WHERE folder_id IS NEW.folder_id AND id != NEW.id
                ))
            WHERE id = NEW.id;
        END""")
    connection.execute("CREATE INDEX IF NOT EXISTS documents_folder_position ON documents(folder_id, position)")
    connection.execute("CREATE INDEX IF NOT EXISTS messages_chat_id ON messages(chat_id, id)")
    connection.execute("CREATE INDEX IF NOT EXISTS messages_pinned_order ON messages(pinned, pinned_at, id)")


def delete_document(name):
    """Delete a document and its chunks atomically via the foreign-key cascade."""
    with database_session(write=True) as connection:
        connection.execute("DELETE FROM documents WHERE name = ?", (name,))


def init_db():
    connection = connect()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            schema = """
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    uploaded_at TEXT NOT NULL,
                    n_chunks INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY,
                    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    page INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    embedding BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chunks_doc_id ON chunks(doc_id);
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY,
                    text TEXT NOT NULL,
                    kind TEXT,
                    created_at TEXT NOT NULL,
                    embedding BLOB
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)
            _migrate(connection)
    finally:
        connection.close()


@contextmanager
def database_session(write=False):
    init_db()
    connection = connect()
    try:
        with connection:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
    finally:
        connection.close()


def normalize_document_ids(source_doc_ids):
    if source_doc_ids is None:
        return None
    ids = list(source_doc_ids)
    if any(type(value) is not int or value < 1 for value in ids):
        raise ValueError("Document IDs must be positive integers.")
    return list(dict.fromkeys(ids))
