"""Workspace-scoped SQL, migrations, file storage and pooled connections."""

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import logging
import mimetypes
from pathlib import Path
import re
import sqlite3
from threading import RLock
import time
from uuid import uuid4

import numpy as np

if not __package__:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "src"
    sys.modules["src.db"] = sys.modules[__name__]

from . import config
from .config import StorageError
from .db_schema import TABLES, VERSION, base_statements, migrate_postgres, migrate_sqlite
from .db_sql import Row, parameter_count, postgres_sql, scoped_sql
from .sqlite_migrations import infer_document_kind

_POOL = None
_POOL_KEY = None
_POOL_LOCK = RLock()
_MIGRATION_LOCK = RLock()
_MIGRATED = set()
_REGISTRY_READY = set()
_VECTOR_LOCK = RLock()
_VECTOR_LOAD_LOCK = RLock()
_VECTORS = OrderedDict()
_SIZE_LOCK = RLock()
_DATABASE_SIZE = (float("-inf"), 0)
_GENERATIONS = {}
_GENERATION_LOCK = RLock()


class StorageLimitError(ValueError):
    public_message = True


def backend():
    return "postgres" if config.get_database_url() else "sqlite"


def _fingerprint(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def current_workspace_id():
    from .workspaces import get_storage

    storage = get_storage()
    return storage.workspace_id or "local-" + _fingerprint(str(storage.root))[:32]


def _pool():
    global _POOL, _POOL_KEY
    url = config.get_database_url()
    if not url:
        raise StorageError()
    key = _fingerprint(url)
    with _POOL_LOCK:
        if _POOL is not None:
            if _POOL_KEY != key:
                raise StorageError()
            return _POOL
        try:
            from psycopg.conninfo import conninfo_to_dict
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            logging.getLogger("psycopg.pool").disabled = True
            logging.getLogger("psycopg").addHandler(logging.NullHandler())
            logging.getLogger("psycopg").propagate = False
            options = conninfo_to_dict(url)
            options.setdefault("sslmode", "require")
            options.update(connect_timeout=10, prepare_threshold=None, row_factory=dict_row, autocommit=True)
            options["options"] = (options.get("options", "") + " -c statement_timeout=20000 -c search_path=public").strip()
            pool = ConnectionPool(min_size=1, max_size=5, kwargs=options, open=False,
                                  check=ConnectionPool.check_connection, timeout=10,
                                  reconnect_timeout=30, name="corvium-storage")
            pool.open()
            _POOL, _POOL_KEY = pool, key
            return pool
        except Exception:
            raise StorageError() from None


def _checkout():
    pool = _pool()
    for attempt in range(3):
        try:
            return pool.getconn(timeout=10), pool
        except Exception:
            if attempt == 2:
                raise StorageError() from None
            time.sleep(0.5 * 2 ** attempt)


def _run(raw, dialect, sql, params=()):
    cursor = None
    try:
        cursor = raw.execute(postgres_sql(sql) if dialect == "postgres" else sql, tuple(params))
        rows = []
        if cursor.description is not None:
            names = [column.name if hasattr(column, "name") else column[0] for column in cursor.description]
            for row in cursor.fetchall():
                rows.append(Row(row) if isinstance(row, dict) else Row(zip(names, row)))
        return _Cursor(rows, cursor.rowcount)
    except Exception:
        raise StorageError() from None
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass


class _Cursor:
    def __init__(self, rows=(), rowcount=0):
        self.rows, self.position, self.rowcount = list(rows), 0, rowcount
        self.lastrowid = self.rows[0].get("id") if self.rows else None

    def fetchone(self):
        if self.position >= len(self.rows):
            return None
        row = self.rows[self.position]
        self.position += 1
        return row

    def fetchall(self):
        rows = self.rows[self.position:]
        self.position = len(self.rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        self.rows = []


class _Connection:
    def __init__(self, raw, dialect, workspace_id=None, pool=None, administrative=False):
        self._raw, self.dialect, self.workspace_id = raw, dialect, workspace_id
        self._pool, self._administrative = pool, administrative
        self.in_transaction, self.locked, self.closed = False, False, False
        self.vector_dirty = False

    def _run(self, sql, params=()):
        return _run(self._raw, self.dialect, sql, params)

    def _begin(self, write=False):
        if not self.in_transaction:
            self._run("BEGIN IMMEDIATE" if write and self.dialect == "sqlite" else "BEGIN")
            self.in_transaction = True
        if write and self.dialect == "postgres" and self.workspace_id and not self.locked:
            self._run("SELECT id FROM workspaces WHERE id = ? FOR UPDATE", (self.workspace_id,))
            self.locked = True

    def _references(self, values):
        for field, table in (("folder_id", "folders"), ("doc_id", "documents"), ("chat_id", "chats")):
            if values.get(field) is not None:
                row = self._run(f"SELECT id FROM {table} WHERE id = ? AND workspace_id = ?", (values[field], self.workspace_id)).fetchone()
                if row is None:
                    raise ValueError("This item is not available in the current workspace.")

    def execute(self, sql, params=()):
        command = sql.strip().upper()
        if command in {"BEGIN", "BEGIN IMMEDIATE"}:
            self._begin(write=True)
            return _Cursor()
        if self._administrative:
            return self._run(sql, params)
        sql, params, operation, table, values = scoped_sql(sql, params, self.workspace_id, self.dialect)
        if operation in {"INSERT", "UPDATE", "DELETE"}:
            self._begin(write=True)
            self._references(values)
            if table == "files" and "data" in values:
                check_upload_guard(self)
                if len(values["data"]) > config.MAX_FILE_BYTES:
                    raise StorageLimitError("This file exceeds the 25 MB limit. Please upload a smaller file.")
                length = "octet_length" if self.dialect == "postgres" else "length"
                previous = self._run(f"SELECT {length}(data) AS size_bytes FROM files WHERE workspace_id = ? AND doc_id = ?", (self.workspace_id, values.get("doc_id"))).fetchone()
                _check_capacity(self, len(values["data"]) - (previous["size_bytes"] if previous else 0))
            if "embedding" in values and values["embedding"] is not None:
                previous = 0
                if operation == "UPDATE":
                    select = re.sub(r"^UPDATE\s+\w+\s+SET.*?WHERE", f"SELECT embedding FROM {table} WHERE", sql, count=1, flags=re.I | re.S)
                    before = parameter_count(sql[:sql.upper().index("WHERE")])
                    old = self._run(select, params[before:]).fetchall()
                    previous = sum(len(row["embedding"] or b"") for row in old)
                _check_capacity(self, len(values["embedding"]) - previous)
            if table in {"documents", "chunks"}:
                self.vector_dirty = True
        result = self._run(sql, params)
        if operation == "INSERT" and table == "documents" and result.lastrowid is not None:
            self._run("UPDATE documents SET display_name = COALESCE(display_name, name), kind = COALESCE(kind, ?), position = COALESCE(position, (SELECT COALESCE(MAX(d.position), -1) + 1 FROM documents d WHERE d.workspace_id = ? AND d.folder_id IS NOT DISTINCT FROM documents.folder_id AND d.id != documents.id)) WHERE id = ? AND workspace_id = ?".replace("IS NOT DISTINCT FROM", "IS" if self.dialect == "sqlite" else "IS NOT DISTINCT FROM"),
                      (infer_document_kind(values.get("name", "")), self.workspace_id, result.lastrowid, self.workspace_id))
        return result

    def executemany(self, sql, parameters):
        count = 0
        for params in parameters:
            count += max(0, self.execute(sql, params).rowcount)
        return _Cursor(rowcount=count)

    def commit(self):
        if self.in_transaction:
            self._run("COMMIT")
            self.in_transaction, self.locked = False, False
            if self.vector_dirty:
                invalidate_vectors(self.workspace_id)
                self.vector_dirty = False

    def rollback(self):
        if self.in_transaction:
            try:
                self._run("ROLLBACK")
            finally:
                self.in_transaction, self.locked = False, False

    def close(self):
        if self.closed:
            return
        try:
            self.rollback()
        finally:
            self.closed = True
            try:
                self._pool.putconn(self._raw) if self._pool is not None else self._raw.close()
            except Exception:
                raise StorageError() from None

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        self.rollback() if kind is not None else self.commit()


def _sqlite_open(path):
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise StorageError("SQLite 3.35 or newer is required for storage.")
    raw = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(path, timeout=30, isolation_level=None)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        return raw
    except Exception:
        if raw is not None:
            try:
                raw.close()
            except Exception:
                pass
        raise StorageError() from None


def _storage():
    from .workspaces import get_storage
    return get_storage()


def init_db():
    dialect = backend()
    if dialect == "sqlite":
        storage = _storage()
        from .workspaces import ensure_workspace, bound_database_identity, database_identity
        if bound_database_identity() is None:
            ensure_workspace(storage)
        elif not storage.db_path.is_file() or database_identity(storage) != bound_database_identity():
            raise StorageError()
    with _MIGRATION_LOCK:
        if dialect == "postgres":
            key = (dialect, _fingerprint(config.get_database_url()), VERSION)
            if key in _MIGRATED:
                return
            raw, pool = _checkout()
            connection = _Connection(raw, dialect, pool=pool, administrative=True)
            try:
                connection._run("SELECT pg_advisory_lock(?)", (724238910,))
                connection._begin(write=True)
                migrate_postgres(connection._run)
                connection.commit()
                _MIGRATED.add(key)
            except BaseException:
                connection.rollback()
                raise
            finally:
                try:
                    connection._run("SELECT pg_advisory_unlock(?)", (724238910,))
                finally:
                    connection.close()
            return
        raw = _sqlite_open(storage.db_path)
        try:
            info = storage.db_path.stat()
            key = (dialect, str(storage.db_path), info.st_ino, VERSION)
            if key not in _MIGRATED:
                migrate_sqlite(raw, current_workspace_id())
                _import_legacy_files(raw, storage)
                _MIGRATED.add(key)
        except StorageError:
            raise
        except Exception:
            raise StorageError() from None
        finally:
            raw.close()


def connect():
    init_db()
    storage, workspace_id = _storage(), current_workspace_id()
    if backend() == "postgres":
        raw, pool = _checkout()
        return _Connection(raw, "postgres", workspace_id, pool)
    from .workspaces import bound_database_identity, database_identity
    identity = bound_database_identity()
    if identity is not None and database_identity(storage) != identity:
        raise StorageError()
    raw = _sqlite_open(storage.db_path)
    return _Connection(raw, "sqlite", workspace_id)


@contextmanager
def database_session(write=False):
    connection = connect()
    try:
        connection._begin(write=write)
        with connection:
            yield connection
    finally:
        connection.close()


@contextmanager
def transaction():
    with database_session(write=True) as connection:
        yield connection


def execute(sql, params=(), fetch=None):
    with database_session(write=not sql.lstrip().upper().startswith("SELECT")) as connection:
        cursor = connection.execute(sql, params)
        if fetch in {"one", "fetchone"}:
            return cursor.fetchone()
        if fetch in {"all", "fetchall", True}:
            return cursor.fetchall()
        return cursor


@contextmanager
def _admin_session(registry=False):
    dialect = backend()
    if dialect == "postgres":
        init_db()
        raw, pool = _checkout()
        connection = _Connection(raw, dialect, pool=pool, administrative=True)
    elif registry:
        path = config.DATA_DIR / "registry.db"
        raw = _sqlite_open(path)
        try:
            with _MIGRATION_LOCK:
                key = (str(path.resolve()), path.stat().st_ino)
                if key not in _REGISTRY_READY:
                    for statement in base_statements("sqlite"):
                        _run(raw, "sqlite", statement)
                    _import_legacy_usage(raw)
                    _REGISTRY_READY.add(key)
        except BaseException:
            raw.close()
            raise
        connection = _Connection(raw, dialect, administrative=True)
    else:
        init_db()
        connection = _Connection(_sqlite_open(_storage().db_path), dialect, administrative=True)
    try:
        connection._begin(write=True)
        with connection:
            yield connection
    finally:
        connection.close()


def embedding_to_blob(vector):
    return np.asarray(vector, dtype=np.float32).tobytes()


def embedding_from_blob(blob):
    return np.frombuffer(blob, dtype=np.float32)


def normalize_document_ids(source_doc_ids):
    if source_doc_ids is None:
        return None
    ids = list(source_doc_ids)
    if any(type(value) is not int or value < 1 for value in ids):
        raise ValueError("Document IDs must be positive integers.")
    return list(dict.fromkeys(ids))


def delete_document(name):
    with transaction() as connection:
        connection.execute("DELETE FROM documents WHERE name = ?", (name,))


def _require_workspace(workspace_id):
    if workspace_id != current_workspace_id():
        raise ValueError("This item is not available in the current workspace.")


def get_file_bytes(workspace_id, doc_id):
    _require_workspace(workspace_id)
    row = execute("SELECT data FROM files WHERE doc_id = ?", (doc_id,), "one")
    return bytes(row["data"]) if row is not None else None


def workspace_bytes(connection):
    length = "octet_length" if connection.dialect == "postgres" else "length"
    total = 0
    for table, column in (("files", "data"), ("chunks", "embedding"), ("memories", "embedding")):
        row = connection._run(f"SELECT COALESCE(SUM({length}({column})), 0) AS size FROM {table} WHERE workspace_id = ?", (connection.workspace_id,)).fetchone()
        total += int(row["size"])
    return total


def _check_capacity(connection, extra):
    if extra > 0 and workspace_bytes(connection) + extra > config.MAX_WORKSPACE_BYTES:
        raise StorageLimitError("The workspace storage limit has been reached. Delete a document before uploading more.")


def _database_size(connection):
    global _DATABASE_SIZE
    with _SIZE_LOCK:
        if time.monotonic() - _DATABASE_SIZE[0] >= 60:
            size = connection._run("SELECT pg_database_size(current_database()) AS size").fetchone()["size"]
            _DATABASE_SIZE = time.monotonic(), int(size)
        return _DATABASE_SIZE[1]


def check_upload_guard(connection=None):
    if backend() != "postgres":
        return
    with _SIZE_LOCK:
        fresh = time.monotonic() - _DATABASE_SIZE[0] < 60
        size = _DATABASE_SIZE[1]
    if not fresh:
        if connection is not None:
            size = _database_size(connection)
        else:
            with _admin_session() as current:
                size = _database_size(current)
    if size > config.DB_SOFT_LIMIT_MB * 1024 * 1024:
        raise StorageLimitError("Uploads are temporarily unavailable because storage is full. Please try again later.")


def check_upload_capacity(connection, file_size, embedding_size=0, replacing_doc_id=None):
    if file_size > config.MAX_FILE_BYTES:
        raise StorageLimitError("This file exceeds the 25 MB limit. Please upload a smaller file.")
    previous = 0
    if replacing_doc_id is not None:
        length = "octet_length" if connection.dialect == "postgres" else "length"
        for table, column in (("files", "data"), ("chunks", "embedding")):
            previous += int(connection.execute(f"SELECT COALESCE(SUM({length}({column})), 0) AS size FROM {table} WHERE doc_id = ?", (replacing_doc_id,)).fetchone()["size"])
    _check_capacity(connection, file_size + embedding_size - previous)


def put_file(connection, doc_id, filename, contents):
    if len(contents) > config.MAX_FILE_BYTES:
        raise StorageLimitError("This file exceeds the 25 MB limit. Please upload a smaller file.")
    previous = connection.execute("SELECT size_bytes FROM files WHERE doc_id = ?", (doc_id,)).fetchone()
    _check_capacity(connection, len(contents) - (previous["size_bytes"] if previous else 0))
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    connection.execute("INSERT INTO files(doc_id, filename, mime, size_bytes, data) VALUES (?, ?, ?, ?, ?) ON CONFLICT(workspace_id, doc_id) DO UPDATE SET filename = excluded.filename, mime = excluded.mime, size_bytes = excluded.size_bytes, data = excluded.data RETURNING id",
                       (doc_id, filename, mime, len(contents), contents))


def _import_legacy_files(raw, storage):
    for row in raw.execute("SELECT id, name, stored_path, workspace_id FROM documents WHERE stored_path IS NOT NULL").fetchall():
        path = Path(row["stored_path"])
        path = path if path.is_absolute() else storage.root / path
        path = path.resolve()
        if not path.is_relative_to(storage.files_dir.resolve()) or not path.is_file():
            continue
        data = path.read_bytes()
        raw.execute("INSERT INTO files(workspace_id, doc_id, filename, mime, size_bytes, data) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(workspace_id, doc_id) DO NOTHING",
                    (row["workspace_id"], row["id"], row["name"], mimetypes.guess_type(row["name"])[0] or "application/octet-stream", len(data), data))
    raw.execute("UPDATE documents SET stored_path = NULL")


def invalidate_vectors(workspace_id):
    with _GENERATION_LOCK:
        _GENERATIONS.pop(workspace_id, None)
    with _VECTOR_LOCK:
        for key in list(_VECTORS):
            if key[0] == workspace_id:
                del _VECTORS[key]


def cached_chunks():
    workspace_id = current_workspace_id()
    with _VECTOR_LOAD_LOCK:
        with database_session() as connection:
            stats = connection.execute("SELECT COUNT(*) AS count, COALESCE(MAX(id), 0) AS last_id FROM chunks").fetchone()
            key = workspace_id, stats["count"], stats["last_id"]
            with _VECTOR_LOCK:
                if key in _VECTORS:
                    _VECTORS.move_to_end(key)
                    return _VECTORS[key]
            rows = connection.execute("SELECT documents.id AS doc_id, documents.display_name AS doc_name, chunks.page, chunks.section, chunks.text, chunks.embedding FROM chunks JOIN documents ON documents.id = chunks.doc_id ORDER BY chunks.id").fetchall()
        vectors = np.stack([embedding_from_blob(row["embedding"]) for row in rows]) if rows else np.empty((0, 4096), dtype=np.float32)
        vectors.setflags(write=False)
        with _VECTOR_LOCK:
            for entry in list(_VECTORS):
                if entry[0] == workspace_id:
                    del _VECTORS[entry]
            _VECTORS[key] = rows, vectors
            while len({entry[0] for entry in _VECTORS}) > 20:
                oldest = next(iter(_VECTORS))[0]
                for entry in list(_VECTORS):
                    if entry[0] == oldest:
                        del _VECTORS[entry]
        return rows, vectors


_TOUCH_LOCK = RLock()
_LAST_TOUCH = {}
_RETENTION_LOCK = RLock()
_LAST_RETENTION = float("-inf")


def _valid_internal_id(workspace_id):
    from .workspaces import validate_workspace_id
    if workspace_id == "demo-template" or re.fullmatch(r"local-[0-9a-f]{32}", workspace_id):
        return workspace_id
    return validate_workspace_id(workspace_id)


def _workspace_connection():
    return _admin_session(registry=backend() == "sqlite")


def _workspace_info(workspace_id):
    _valid_internal_id(workspace_id)
    with _workspace_connection() as connection:
        return connection._run("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()


def _workspace_generation(workspace_id):
    with _GENERATION_LOCK:
        cached = _GENERATIONS.get(workspace_id)
        if cached is not None and time.monotonic() - cached[0] < 5:
            return cached[1]
    row = _workspace_info(workspace_id)
    if row is None:
        raise StorageError()
    identity = ("postgres", workspace_id, row["generation"])
    with _GENERATION_LOCK:
        _GENERATIONS[workspace_id] = time.monotonic(), identity
    return identity


def _make_workspace(workspace_id, ready=1):
    _valid_internal_id(workspace_id)
    now = datetime.now(timezone.utc).isoformat()
    with _workspace_connection() as connection:
        connection._run("INSERT INTO workspaces(id, created_at, last_active_at, generation, ready) VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                        (workspace_id, now, now, uuid4().hex, ready))


def create_workspace(workspace_id):
    from .workspaces import validate_workspace_id, storage_context, workspace_storage
    validate_workspace_id(workspace_id)
    if backend() == "postgres":
        _make_workspace(workspace_id)
    else:
        storage = workspace_storage(workspace_id)
        storage.root.mkdir(parents=True, exist_ok=True)
        with storage_context(storage.root, workspace_id):
            raw = _sqlite_open(storage.db_path)
            try:
                migrate_sqlite(raw, workspace_id)
            except StorageError:
                raise
            except Exception:
                raise StorageError() from None
            finally:
                raw.close()
        _make_workspace(workspace_id)


def _touch_record(workspace_id, now=None):
    _valid_internal_id(workspace_id)
    with _TOUCH_LOCK:
        if now is None and time.monotonic() - _LAST_TOUCH.get(workspace_id, -1e9) < 300:
            return
        timestamp = time.time() if now is None else now
        text = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        with _workspace_connection() as connection:
            connection._run("INSERT INTO workspaces(id, created_at, last_active_at, generation, ready) VALUES (?, ?, ?, ?, 1) ON CONFLICT(id) DO UPDATE SET last_active_at = excluded.last_active_at",
                            (workspace_id, text, text, uuid4().hex))
        _LAST_TOUCH[workspace_id] = time.monotonic()


@contextmanager
def template_lock():
    if backend() == "postgres":
        with _admin_session() as connection:
            connection._run("SELECT pg_advisory_xact_lock(?)", (724238911,))
            yield
    else:
        from .workspaces import file_lock
        with file_lock(config.DATA_DIR / "demo_template.lock"):
            yield


def _clear_rows(connection, workspace_id):
    for table in reversed(TABLES):
        connection._run(f"DELETE FROM {table} WHERE workspace_id = ?", (workspace_id,))


def _remap_json(value, maps):
    import json

    def walk(item):
        if isinstance(item, list):
            return [walk(value) for value in item]
        if isinstance(item, dict):
            result = {}
            for key, value in item.items():
                if key == "doc_id" and type(value) is int:
                    value = maps["documents"].get(value, value)
                elif key in {"doc_ids", "source_doc_ids"} and isinstance(value, list):
                    value = [maps["documents"].get(entry, entry) for entry in value]
                else:
                    value = walk(value)
                result[key] = value
            return result
        return item
    try:
        return json.dumps(walk(json.loads(value)), ensure_ascii=False)
    except (ValueError, TypeError):
        return value


def _clone_rows(connection, source_id, target_id):
    maps = {table: {} for table in TABLES}
    references = {"folder_id": "folders", "doc_id": "documents", "chat_id": "chats"}
    for table in TABLES:
        rows = connection._run(f"SELECT * FROM {table} WHERE workspace_id = ? ORDER BY id", (source_id,)).fetchall()
        for row in rows:
            original = row["id"]
            values = dict(row)
            del values["id"]
            values["workspace_id"] = target_id
            for field, parent in references.items():
                if values.get(field) is not None:
                    values[field] = maps[parent][values[field]]
            if table == "messages":
                values["session_id"] = str(values["chat_id"])
                values["sources"] = _remap_json(values["sources"], maps)
            if table == "plans":
                values["body_json"] = _remap_json(values["body_json"], maps)
            if table == "documents":
                values["stored_path"] = None
            columns = list(values)
            row = connection._run(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) RETURNING id", tuple(values[column] for column in columns)).fetchone()
            maps[table][original] = row["id"]
    return maps


def clone_workspace(template_id, new_id):
    from .workspaces import validate_workspace_id, workspace_storage, storage_context
    validate_workspace_id(new_id)
    if template_id != "demo-template":
        _require_workspace(template_id)
    if backend() == "sqlite":
        from .workspaces import ensure_template, _restore_locked
        source = ensure_template() if template_id == "demo-template" else workspace_storage(template_id).root
        storage = workspace_storage(new_id)
        if storage.db_path.exists():
            raise ValueError("This workspace already exists.")
        _restore_locked(storage, source)
        _touch_record(new_id)
        return
    now = datetime.now(timezone.utc).isoformat()
    with _admin_session() as connection:
        source = connection._run("SELECT id FROM workspaces WHERE id = ? AND ready = 1 FOR SHARE", (template_id,)).fetchone()
        if source is None:
            raise StorageError()
        if connection._run("SELECT id FROM workspaces WHERE id = ?", (new_id,)).fetchone() is not None:
            raise ValueError("This workspace already exists.")
        created = connection._run("INSERT INTO workspaces(id, created_at, last_active_at, generation, ready) VALUES (?, ?, ?, ?, 1) ON CONFLICT(id) DO NOTHING RETURNING id", (new_id, now, now, uuid4().hex)).fetchone()
        if created is None:
            raise ValueError("This workspace already exists.")
        _clone_rows(connection, template_id, new_id)
    invalidate_vectors(new_id)


def reset_workspace(workspace_id):
    _require_workspace(workspace_id)
    if backend() != "postgres":
        from .workspaces import ensure_template, _restore_locked, get_storage
        _restore_locked(get_storage(), ensure_template())
        invalidate_vectors(workspace_id)
        return
    from .workspaces import ensure_template
    ensure_template()
    with _admin_session() as connection:
        connection._run("SELECT id FROM workspaces WHERE id = ? FOR UPDATE", (workspace_id,))
        _clear_rows(connection, workspace_id)
        _clone_rows(connection, "demo-template", workspace_id)
        connection._run("UPDATE workspaces SET generation = ?, last_active_at = ? WHERE id = ?",
                        (uuid4().hex, datetime.now(timezone.utc).isoformat(), workspace_id))
    invalidate_vectors(workspace_id)


def delete_workspace(workspace_id):
    _require_workspace(workspace_id)
    if backend() == "sqlite":
        from .workspaces import get_storage, _remove_tree, file_lock
        storage = get_storage()
        with _admin_session() as connection:
            connection._run("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
        with _workspace_connection() as connection:
            connection._run("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
        with file_lock(storage.root.parent / (workspace_id + ".lock")):
            _remove_tree(storage.root, storage.root.parent)
    else:
        with _workspace_connection() as connection:
            connection._run("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
    invalidate_vectors(workspace_id)


def cleanup_inactive_workspaces(now=None, exclude=()):
    global _LAST_RETENTION
    with _RETENTION_LOCK:
        if now is None and time.monotonic() - _LAST_RETENTION < 3600:
            return []
        cutoff = datetime.fromtimestamp((time.time() if now is None else now) - config.WORKSPACE_RETENTION_DAYS * 86400, timezone.utc).isoformat()
        removed = []
        if backend() == "sqlite":
            base = config.DATA_DIR / "workspaces"
            if base.is_dir():
                for path in base.iterdir():
                    if re.fullmatch(r"[0-9a-f]{32}", path.name) is None or path.is_symlink() or path.is_junction():
                        continue
                    if _workspace_info(path.name) is None:
                        marker = path / ".last_used"
                        try:
                            _touch_record(path.name, (marker if marker.exists() else path).stat().st_mtime)
                        except OSError:
                            continue
        with _workspace_connection() as connection:
            if connection.dialect == "postgres":
                connection._run("SELECT pg_advisory_xact_lock(?)", (724238912,))
            rows = connection._run("SELECT id FROM workspaces WHERE last_active_at < ? AND id != ?", (cutoff, "demo-template")).fetchall()
            for row in rows:
                if row["id"] in exclude or re.fullmatch(r"[0-9a-f]{32}", row["id"]) is None:
                    continue
                result = connection._run("DELETE FROM workspaces WHERE id = ? AND last_active_at < ? RETURNING id", (row["id"], cutoff)).fetchone()
                if result:
                    removed.append(row["id"])
        for workspace_id in removed:
            invalidate_vectors(workspace_id)
        _LAST_RETENTION = time.monotonic()
        return removed


def rebind_sqlite_workspace(path, workspace_id):
    raw = _sqlite_open(path)
    try:
        migrate_sqlite(raw, workspace_id)
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("BEGIN IMMEDIATE")
        for table in TABLES:
            raw.execute(f"UPDATE {table} SET workspace_id = ?", (workspace_id,))
        raw.execute("DELETE FROM usage_events")
        raw.execute("DELETE FROM workspaces")
        now = datetime.now(timezone.utc).isoformat()
        raw.execute("INSERT INTO workspaces(id, created_at, last_active_at, generation, ready) VALUES (?, ?, ?, ?, 1)", (workspace_id, now, now, uuid4().hex))
        raw.commit()
        raw.execute("PRAGMA foreign_keys = ON")
        if raw.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StorageError()
    except BaseException as error:
        raw.rollback()
        if isinstance(error, sqlite3.Error):
            raise StorageError() from None
        raise
    finally:
        raw.close()
    invalidate_vectors(workspace_id)


def _import_legacy_usage(raw):
    old = config.DATA_DIR / "usage.db"
    raw.execute("BEGIN IMMEDIATE")
    try:
        if raw.execute("SELECT 1 FROM schema_version WHERE version = ?", (VERSION,)).fetchone():
            raw.commit()
            return
        if old.is_file():
            source = sqlite3.connect(old.as_uri() + "?mode=ro", uri=True)
            try:
                source.execute("PRAGMA foreign_keys = ON")
                for workspace_id, kind, occurred_at in source.execute("SELECT workspace_id, kind, occurred_at FROM usage_events"):
                    created = datetime.fromtimestamp(occurred_at, timezone.utc).isoformat()
                    if workspace_id is not None:
                        raw.execute("INSERT INTO workspaces(id, created_at, last_active_at, generation, ready) VALUES (?, ?, ?, ?, 1) ON CONFLICT(id) DO NOTHING", (workspace_id, created, created, uuid4().hex))
                    raw.execute("INSERT INTO usage_events(workspace_id, kind, created_at) VALUES (?, ?, ?)", (workspace_id, kind, created))
            finally:
                source.close()
        raw.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?) ON CONFLICT(version) DO NOTHING", (VERSION, datetime.now(timezone.utc).isoformat()))
        raw.commit()
    except BaseException:
        raw.rollback()
        raise StorageError() from None


def touch_workspace(workspace_id, now=None):
    _require_workspace(workspace_id)
    _touch_record(workspace_id, now)
