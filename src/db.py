"""SQLite storage; vectors are serialized as float32 bytes."""

import sqlite3

import numpy as np

if __package__:
    from .config import DB_PATH
else:
    from config import DB_PATH


def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def embedding_to_blob(vector):
    return np.asarray(vector, dtype=np.float32).tobytes()


def embedding_from_blob(blob):
    return np.frombuffer(blob, dtype=np.float32)


def delete_document(name):
    """Delete a document and its chunks atomically via the foreign-key cascade."""
    connection = connect()
    try:
        with connection:
            connection.execute("DELETE FROM documents WHERE name = ?", (name,))
    finally:
        connection.close()


def init_db():
    connection = connect()
    try:
        with connection:
            connection.executescript("""
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
            """)
    finally:
        connection.close()
