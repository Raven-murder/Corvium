from functools import wraps

import streamlit as st

if __package__:
    from . import db
    from .workspaces import get_storage
else:
    import db
    from workspaces import get_storage


def cached_list(function):
    def load(workspace_id, data_version):
        if workspace_id != db.current_workspace_id():
            raise ValueError("This data is unavailable in the current workspace.")
        return function()

    load.__name__ = function.__name__
    load.__qualname__ = function.__module__ + "." + function.__qualname__
    cached = st.cache_data(show_spinner=False, ttl=60, max_entries=256)(load)

    @wraps(function)
    def read():
        from src.jobs import data_version
        scope = db._read_scope()
        if scope is not None and scope.busy is not None:
            return function()
        workspace_id = db.current_workspace_id()
        if db.backend() == "sqlite":
            path = get_storage().db_path
            def stamp(target):
                try:
                    value = target.stat()
                    return value.st_ino, value.st_mtime_ns, value.st_size
                except OSError:
                    return None
            identity = stamp(path), stamp(path.with_name(path.name + "-wal"))
        else:
            identity = db.known_workspace_identity(workspace_id)
        return cached(workspace_id, (data_version(workspace_id), identity))
    return read
