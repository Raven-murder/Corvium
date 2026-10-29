from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4

if not __package__:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "src"
    sys.modules["src.workspaces"] = sys.modules[__name__]

if __package__:
    from . import config
else:
    import config

_STORAGE = ContextVar("workspace_storage", default=None)
_DATABASE_IDENTITY = ContextVar("workspace_database_identity", default=None)
_ID = re.compile(r"[0-9a-f]{32}")


class WorkspaceError(RuntimeError):
    public_message = True


def validate_workspace_id(value):
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise WorkspaceError("This workspace is unavailable. Please refresh the page.")
    return value


def resolve_workspace_id(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else uuid4().hex


@dataclass(frozen=True)
class Storage:
    root: Path
    workspace_id: str | None = None

    def child(self, name):
        path = self.root / name
        if path.resolve() != path.absolute():
            raise WorkspaceError("This workspace is unavailable. Please refresh the page.")
        return path

    @property
    def db_path(self):
        return self.child("app.db")

    @property
    def files_dir(self):
        return self.child("files")

    @property
    def uploads_dir(self):
        return self.child("uploads")


def workspace_storage(workspace_id):
    workspace_id = validate_workspace_id(workspace_id)
    base = (config.DATA_DIR / "workspaces").resolve()
    root = base / workspace_id
    if root.resolve() != root or root.is_symlink() or root.is_junction():
        raise WorkspaceError("This workspace is unavailable. Please refresh the page.")
    return Storage(root, workspace_id)


@contextmanager
def storage_context(root, workspace_id=None):
    if workspace_id is not None:
        expected = workspace_storage(workspace_id)
        if Path(root).resolve() != expected.root:
            raise WorkspaceError("This workspace is unavailable. Please refresh the page.")
    token = _STORAGE.set(Storage(Path(root).resolve(), workspace_id))
    try:
        yield _STORAGE.get()
    finally:
        _STORAGE.reset(token)


def database_identity(storage):
    from . import db
    if db.backend() == "postgres":
        return db._workspace_generation(storage.workspace_id)
    metadata = storage.db_path.stat()
    return metadata.st_dev, metadata.st_ino


@contextmanager
def workspace_context(workspace_id, expected_database=None):
    """Bind a validated existing workspace independently of a Streamlit session."""
    storage = workspace_storage(workspace_id)
    identity = database_identity(storage)
    if expected_database is not None and identity != expected_database:
        raise WorkspaceError("This workspace changed while the answer was running.")
    with storage_context(storage.root, workspace_id):
        token = _DATABASE_IDENTITY.set(identity)
        try:
            yield storage
        finally:
            _DATABASE_IDENTITY.reset(token)


def bound_database_identity():
    return _DATABASE_IDENTITY.get()


def get_storage():
    storage = _STORAGE.get()
    if storage is not None:
        return storage
    import streamlit as st
    from streamlit.runtime.scriptrunner import get_script_run_ctx

    if get_script_run_ctx(suppress_warning=True) is None:
        raise WorkspaceError("Open the app to select a workspace first.")
    return workspace_storage(validate_workspace_id(st.query_params.get("w")))


@contextmanager
def file_lock(path, timeout=60):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        end = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= end:
                    raise WorkspaceError("The app is busy preparing data. Please try again shortly.") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _remove_tree(path, parent):
    parent = Path(parent).resolve()
    path = Path(path)
    resolved = path.resolve()
    if resolved == parent or resolved.parent != parent or resolved != path.absolute() or path.is_symlink() or path.is_junction():
        raise WorkspaceError("The app could not safely update its storage.")
    if path.exists():
        shutil.rmtree(path)


def _touch(storage, now=None):
    from . import db
    if storage.workspace_id:
        db._touch_record(storage.workspace_id, now)
    if db.backend() == "sqlite":
        marker = storage.root / ".last_used"
        marker.touch(exist_ok=True)
        if now is not None:
            os.utime(marker, (now, now))


def template_directory():
    return config.DATA_DIR / "demo_template"


@contextmanager
def _template_context(root):
    token = _STORAGE.set(Storage(Path(root).resolve(), "demo-template"))
    try:
        yield _STORAGE.get()
    finally:
        _STORAGE.reset(token)


def ensure_template():
    from . import db, demo

    if db.backend() == "postgres":
        db.init_db()
        with db.template_lock():
            record = db._workspace_info("demo-template")
            if record is not None and record["ready"]:
                return "demo-template"
            db._make_workspace("demo-template", ready=0)
            with db._admin_session() as connection:
                db._clear_rows(connection, "demo-template")
            with _template_context(config.DATA_DIR / "demo_template"):
                results = demo._seed_demo_files() or []
                if any(result["error"] for result in results):
                    raise WorkspaceError("The demo could not be prepared. Please try again later.")
            with db._admin_session() as connection:
                connection._run("UPDATE workspaces SET ready = 1 WHERE id = ?", ("demo-template",))
        return "demo-template"
    template = template_directory()
    with file_lock(config.DATA_DIR / "demo_template.lock"):
        if (template / ".ready").is_file() and (template / "app.db").is_file():
            with _template_context(template):
                db.init_db()
            return template
        stage = config.DATA_DIR / (".demo-template-" + uuid4().hex)
        stage.mkdir(parents=True)
        try:
            with _template_context(stage):
                db.init_db()
                results = demo._seed_demo_files() or []
                if any(result["error"] for result in results):
                    raise WorkspaceError("The demo could not be prepared. Please try again later.")
                (stage / "files").mkdir(exist_ok=True)
            (stage / ".ready").write_text("ready", encoding="utf-8")
            if template.exists():
                _remove_tree(template, config.DATA_DIR)
            stage.replace(template)
        except BaseException:
            _remove_tree(stage, config.DATA_DIR)
            raise
    return template


def _restore_locked(storage, template):
    base = storage.root.parent
    stage = base / (".copy-" + uuid4().hex)
    backup = base / (".restore-" + uuid4().hex)
    try:
        shutil.copytree(template, stage)
        from .db import rebind_sqlite_workspace
        rebind_sqlite_workspace(stage / "app.db", storage.workspace_id)
        _touch(Storage(stage))
        if storage.root.exists():
            storage.root.replace(backup)
        try:
            stage.replace(storage.root)
        except Exception:
            if backup.exists():
                backup.replace(storage.root)
            raise
        _remove_tree(backup, base)
    finally:
        _remove_tree(stage, base)


def ensure_workspace(storage):
    from . import db

    if storage.workspace_id is None or storage.workspace_id == "demo-template":
        if db.backend() == "sqlite":
            storage.root.mkdir(parents=True, exist_ok=True)
        return
    expected = workspace_storage(storage.workspace_id)
    if storage != expected:
        raise WorkspaceError("This workspace is unavailable. Please refresh the page.")
    if db.backend() == "postgres":
        db.init_db()
        if db._workspace_info(storage.workspace_id) is None:
            ensure_template()
            try:
                db.clone_workspace("demo-template", storage.workspace_id)
            except ValueError:
                if db._workspace_info(storage.workspace_id) is None:
                    raise
        _touch(storage)
        return
    template = ensure_template() if not storage.db_path.is_file() else None
    with file_lock(storage.root.parent / (storage.workspace_id + ".lock")):
        if not storage.db_path.is_file():
            if template is None:
                template = ensure_template()
            _restore_locked(storage, template)
        _touch(storage)


def restore_workspace():
    from . import db
    storage = get_storage()
    if storage.workspace_id is None:
        raise WorkspaceError("Open the app to select a workspace first.")
    if db.backend() == "postgres":
        db.reset_workspace(storage.workspace_id)
    else:
        template = ensure_template()
        with file_lock(storage.root.parent / (storage.workspace_id + ".lock")):
            _restore_locked(storage, template)


def cleanup_workspaces(now=None, exclude=()):
    from . import db
    removed = db.cleanup_inactive_workspaces(now, exclude)
    if db.backend() != "sqlite":
        return
    base = (config.DATA_DIR / "workspaces").resolve()
    for workspace_id in removed:
        try:
            storage = workspace_storage(workspace_id)
            with file_lock(base / (workspace_id + ".lock"), timeout=0):
                _remove_tree(storage.root, base)
        except (OSError, WorkspaceError):
            try:
                marker = storage.root / ".last_used"
                db._touch_record(workspace_id, (marker if marker.exists() else storage.root).stat().st_mtime)
            except OSError:
                pass


def bind_workspace(query_params, state):
    workspace_id = resolve_workspace_id(query_params.get("w"))
    if query_params.get("w") != workspace_id:
        query_params["w"] = workspace_id
    unchanged = state.get("workspace_id") == workspace_id
    if not unchanged:
        state.clear()
        state["workspace_id"] = workspace_id
    return workspace_id, unchanged


def resolve_original_path(stored_path):
    from . import db
    if db.backend() == "postgres" or not stored_path:
        return None
    storage = get_storage()
    path = Path(stored_path)
    if not path.is_absolute():
        path = storage.root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(storage.files_dir.resolve()):
        return None
    return resolved
