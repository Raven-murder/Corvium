from datetime import datetime, timezone
from uuid import uuid4

from .config import StorageError
from .sqlite_migrations import migrate_legacy

VERSION = 3
TABLES = ("folders", "documents", "chunks", "memories", "chats", "messages", "plans", "notes", "bookmarks", "files")


def table_definitions(dialect):
    pk = "BIGSERIAL PRIMARY KEY" if dialect == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    reference = "BIGINT" if dialect == "postgres" else "INTEGER"
    blob = "BYTEA" if dialect == "postgres" else "BLOB"
    workspace = "workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE"
    definitions = {
        "folders": f"id {pk}, {workspace}, name TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0",
        "documents": f"id {pk}, {workspace}, name TEXT NOT NULL, uploaded_at TEXT NOT NULL, n_chunks INTEGER NOT NULL, folder_id {reference} REFERENCES folders(id) ON DELETE SET NULL, position INTEGER, display_name TEXT, kind TEXT, stored_path TEXT, status TEXT NOT NULL DEFAULT 'indexed', UNIQUE(workspace_id, name), FOREIGN KEY(workspace_id, folder_id) REFERENCES folders(workspace_id, id) DEFERRABLE INITIALLY DEFERRED",
        "chunks": f"id {pk}, {workspace}, doc_id {reference} NOT NULL, page INTEGER NOT NULL, text TEXT NOT NULL, embedding {blob} NOT NULL, section TEXT, FOREIGN KEY(workspace_id, doc_id) REFERENCES documents(workspace_id, id) ON DELETE CASCADE",
        "memories": f"id {pk}, {workspace}, text TEXT NOT NULL, kind TEXT, created_at TEXT NOT NULL, embedding {blob}",
        "chats": f"id {pk}, {workspace}, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0, pinned_at TEXT",
        "messages": f"id {pk if dialect == 'postgres' else 'INTEGER PRIMARY KEY'}, {workspace}, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, chat_id {reference}, sources TEXT NOT NULL DEFAULT '[]', error INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'text', pinned INTEGER NOT NULL DEFAULT 0, pinned_at TEXT, FOREIGN KEY(workspace_id, chat_id) REFERENCES chats(workspace_id, id) ON DELETE CASCADE",
        "plans": f"id {pk}, {workspace}, title TEXT NOT NULL, body_json TEXT NOT NULL, days INTEGER NOT NULL, created_at TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0",
        "notes": f"id {pk}, {workspace}, title TEXT NOT NULL, body TEXT NOT NULL, doc_id {reference} REFERENCES documents(id) ON DELETE SET NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(workspace_id, doc_id) REFERENCES documents(workspace_id, id) DEFERRABLE INITIALLY DEFERRED",
        "bookmarks": f"id {pk}, {workspace}, doc_id {reference} NOT NULL, locator TEXT NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(workspace_id, doc_id) REFERENCES documents(workspace_id, id) ON DELETE CASCADE",
        "files": f"id {pk}, {workspace}, doc_id {reference} NOT NULL, filename TEXT NOT NULL, mime TEXT NOT NULL, size_bytes BIGINT NOT NULL, data {blob} NOT NULL, UNIQUE(workspace_id, doc_id), FOREIGN KEY(workspace_id, doc_id) REFERENCES documents(workspace_id, id) ON DELETE CASCADE",
    }
    return {name: body + ", UNIQUE(workspace_id, id)" for name, body in definitions.items()}


def base_statements(dialect):
    pk = "BIGSERIAL PRIMARY KEY" if dialect == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    return [
        "CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, last_active_at TEXT NOT NULL, generation TEXT NOT NULL, ready INTEGER NOT NULL DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS usage_events (id {pk}, workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE, kind TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS usage_lookup ON usage_events(workspace_id, kind, created_at)",
    ]


def index_statements():
    statements = []
    for table in TABLES:
        statements.append(f"CREATE INDEX IF NOT EXISTS {table}_workspace ON {table}(workspace_id)")
    for table in ("chunks", "files", "notes", "bookmarks"):
        statements.append(f"CREATE INDEX IF NOT EXISTS {table}_workspace_document ON {table}(workspace_id, doc_id)")
    statements += ["CREATE INDEX IF NOT EXISTS messages_workspace_chat ON messages(workspace_id, chat_id, id)",
                   "CREATE INDEX IF NOT EXISTS documents_folder_position ON documents(workspace_id, folder_id, position)",
                   "CREATE INDEX IF NOT EXISTS messages_pinned_order ON messages(workspace_id, pinned, pinned_at, id)"]
    return statements


def migrate_postgres(run):
    for statement in base_statements("postgres"):
        run(statement)
    for table, definition in table_definitions("postgres").items():
        run(f"CREATE TABLE IF NOT EXISTS {table} ({definition})")
    for statement in index_statements():
        run(statement)
    for table in (*TABLES, "workspaces", "usage_events", "schema_version"):
        run(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        policies = run("SELECT policyname FROM pg_policies WHERE schemaname = 'public' AND tablename = ?", (table,)).fetchall()
        for policy in policies:
            name = policy["policyname"].replace('"', '""')
            run(f'DROP POLICY IF EXISTS "{name}" ON "{table}"')
    run("INSERT INTO schema_version(version, applied_at) VALUES (?, ?) ON CONFLICT(version) DO NOTHING",
        (VERSION, datetime.now(timezone.utc).isoformat()))


def migrate_sqlite(connection, workspace_id):
    """Rebuild legacy tables atomically to enforce workspace keys without losing rows."""
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in base_statements("sqlite"):
            connection.execute(statement)
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        if version >= VERSION:
            connection.commit()
            return
        for statement in (
            "CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, uploaded_at TEXT NOT NULL, n_chunks INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, page INTEGER NOT NULL, text TEXT NOT NULL, embedding BLOB NOT NULL)",
            "CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, text TEXT NOT NULL, kind TEXT, created_at TEXT NOT NULL, embedding BLOB)",
            "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)",
        ):
            connection.execute(statement)
        migrate_legacy(connection)
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("INSERT INTO workspaces(id, created_at, last_active_at, generation, ready) VALUES (?, ?, ?, ?, 1) ON CONFLICT(id) DO NOTHING",
                           (workspace_id, now, now, uuid4().hex))
        existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table, definition in table_definitions("sqlite").items():
            temporary = "_v3_" + table
            connection.execute(f"CREATE TABLE {temporary} ({definition})")
            if table in existing:
                columns = [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]
                target = columns if "workspace_id" in columns else columns + ["workspace_id"]
                select = ", ".join(columns) + ("" if "workspace_id" in columns else ", ?")
                connection.execute(f"INSERT INTO {temporary} ({', '.join(target)}) SELECT {select} FROM {table}",
                                   () if "workspace_id" in columns else (workspace_id,))
        for table in reversed(TABLES):
            if table in existing:
                connection.execute(f"DROP TABLE {table}")
        for table in TABLES:
            connection.execute(f"ALTER TABLE _v3_{table} RENAME TO {table}")
        for statement in index_statements():
            connection.execute(statement)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StorageError()
        connection.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?)", (VERSION, now))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
