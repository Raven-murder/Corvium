"""Opt-in, parameter-free console profiling for script and fragment reruns."""

import os

ENABLED = os.getenv("CORVIUM_PROFILE") == "1"

if ENABLED:
    from collections import OrderedDict, deque
    from contextvars import ContextVar
    from functools import wraps
    import hashlib
    import heapq
    import io
    import json
    import re
    import sqlite3
    import sys
    import threading
    from time import perf_counter, monotonic

    _CURRENT = ContextVar("corvium_profile_run", default=None)
    _SQL_DEPTH = ContextVar("corvium_profile_sql_depth", default=0)
    _OWNERS = OrderedDict()
    _OWNERS_LOCK = threading.RLock()
    _PRINT_LOCK = threading.Lock()
    _IO_LOCAL = threading.local()
    _SQL_TOKENS = re.compile(r"--[^\r\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\$(?P<tag>[A-Za-z_][A-Za-z_0-9]*|)\$.*?\$(?P=tag)\$|\b\d+(?:\.\d+)?\b", re.S)


class _Nothing:
    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False


_NOTHING = _Nothing()


def _identity(function):
    return function


def _safe_sql(sql):
    try:
        if not isinstance(sql, str):
            return "[SQL unavailable]"
        if re.search(r"(?:postgres(?:ql)?://|password\s*=|api[_-]?key\s*=)", sql, re.I):
            return "[SQL redacted]"
        masked = _SQL_TOKENS.sub("?", sql)
        if any(token in masked for token in ("'", '"', "/*", "--", "$")):
            return "[SQL redacted]"
        return " ".join(masked.split())[:80]
    except Exception:
        return "[SQL redacted]"


class _Metrics:
    def __init__(self):
        self.lock = threading.RLock()
        self.clear()

    def clear(self):
        self.statements, self.db_seconds = 0, 0.0
        self.queries, self.slowest = {}, []
        self.stages, self.operations = {}, {}
        self.setup, self.migrations, self.pool = False, False, False
        self.sequence = 0

    def sql(self, sql, elapsed, count=1):
        if count < 1:
            return
        safe = _safe_sql(sql)
        fingerprint = hashlib.sha256(sql.encode("utf-8", errors="replace")).digest() if isinstance(sql, str) else b"unknown"
        with self.lock:
            self.statements += count
            self.db_seconds += elapsed
            query = self.queries.setdefault(fingerprint, {"sql": safe, "count": 0})
            query["count"] += count
            self.sequence += 1
            item = (elapsed, self.sequence, safe, count)
            heapq.heappush(self.slowest, item)
            if len(self.slowest) > 5:
                heapq.heappop(self.slowest)

    def timing(self, label, elapsed, stage=False):
        with self.lock:
            target = self.stages if stage else self.operations
            value = target.setdefault(label, [0, 0.0])
            value[0] += 1
            value[1] += elapsed

    def snapshot(self, reset=False):
        with self.lock:
            repeated = sorted((query for query in self.queries.values() if query["count"] > 1), key=lambda query: -query["count"])
            checkouts = [value for name, value in self.operations.items() if name in {"db.checkout.sqlite", "db.checkout.postgres"}]
            result = {
                "db": {"statements": self.statements, "time_ms": round(self.db_seconds * 1000, 3),
                       "checkout_count": sum(value[0] for value in checkouts),
                       "checkout_ms": round(sum(value[1] for value in checkouts) * 1000, 3),
                       "slowest": [{"sql": sql, "ms": round(seconds * 1000, 3), "executions": count}
                                   for seconds, _, sql, count in sorted(self.slowest, reverse=True)],
                       "repeated_identical": sum(query["count"] - 1 for query in repeated),
                       "repeated_queries": [dict(query) for query in repeated[:5]]},
                "stages": {name: {"calls": value[0], "ms": round(value[1] * 1000, 3)} for name, value in self.stages.items()},
                "operations": {name: {"calls": value[0], "ms": round(value[1] * 1000, 3)} for name, value in self.operations.items()},
                "setup_ran": "yes" if self.setup else "no", "migrations_ran": "yes" if self.migrations else "no",
                "pool_created": "yes" if self.pool else "no",
            }
            if reset:
                self.clear()
            return result


class _Owner:
    def __init__(self):
        self.lock = threading.RLock()
        self.pending = _Metrics()
        self.history = deque()
        self.active = {}
        self.sequence = 0


class _Run:
    def __init__(self, owner, kind, background=False):
        self.owner, self.kind, self.background = owner, kind, background
        self.metrics = owner.pending if background else _Metrics()
        self.started = perf_counter()
        self.pending_callback = False
        self.finished = False


def _owner():
    from streamlit.runtime.scriptrunner import get_script_run_ctx
    context = get_script_run_ctx(suppress_warning=True)
    key = context.session_id if context is not None else threading.get_ident()
    with _OWNERS_LOCK:
        value = _OWNERS.get(key)
        if value is None:
            value = _OWNERS[key] = _Owner()
        _OWNERS.move_to_end(key)
        while len(_OWNERS) > 256:
            _OWNERS.popitem(last=False)
        return value


def _ensure_file_profile():
    if sys.getprofile() is None:
        sys.setprofile(_file_profile)


def _file_profile(frame, event, function):
    try:
        run = _CURRENT.get()
        if run is None or run.finished or event not in {"c_call", "c_return", "c_exception"}:
            return
        name = getattr(function, "__name__", "")
        target = getattr(function, "__self__", None)
        category = None
        module = getattr(function, "__module__", "")
        if name == "open" and module in {"io", "_io"}:
            category = "disk.open"
        elif module in {"nt", "posix", "os"} and name in {"stat", "lstat", "scandir", "listdir", "readlink", "_getfinalpathname", "_getvolumepathname"}:
            category = "disk.metadata"
        elif name in {"read", "read1", "readinto", "readinto1", "readall", "readline", "readlines"}:
            raw = target
            if isinstance(raw, io.TextIOWrapper):
                raw = raw.buffer
            if isinstance(raw, (io.BufferedReader, io.BufferedRandom)):
                raw = raw.raw
            if isinstance(raw, io.FileIO) and isinstance(raw.name, (str, bytes)):
                category = "disk.read"
        if category is None:
            return
        stack = getattr(_IO_LOCAL, "stack", None)
        if stack is None:
            stack = _IO_LOCAL.stack = []
        if event == "c_call":
            stack.append((function, category, perf_counter(), run))
        elif stack and stack[-1][0] == function:
            _, label, started, source = stack.pop()
            source.metrics.timing(label, perf_counter() - started)
    except Exception:
        return


def begin_script():
    if not ENABLED:
        return
    _ensure_file_profile()
    run = _CURRENT.get()
    if run is None or run.finished:
        run = _Run(_owner(), "script")
        _CURRENT.set(run)
    else:
        run.kind = "script"
        run.pending_callback = False


def finish_script():
    if ENABLED:
        run = _CURRENT.get()
        if run is not None and not run.background and not run.finished:
            _finish(run)


def _finish(run):
    run.finished = True
    elapsed = perf_counter() - run.started
    _CURRENT.set(None)
    now = monotonic()
    with run.owner.lock:
        run.owner.history.append((now, run.kind))
        while run.owner.history and run.owner.history[0][0] < now - 10:
            run.owner.history.popleft()
        count = len(run.owner.history)
        full = sum(kind == "script" for _, kind in run.owner.history)
        inflight = [{"operation": label, "elapsed_ms": round((perf_counter() - started) * 1000, 3)}
                    for label, started in run.owner.active.values()]
    summary = {"event": "corvium_profile", "kind": run.kind, "wall_ms": round(elapsed * 1000, 3),
               "reruns_last_10s": count, "full_reruns_last_10s": full, "suspected_rerun_loop": full >= 5 or count >= 40,
               "timing_scope": "inclusive spans; background completions reported separately",
               "disk_timing_active": sys.getprofile() is _file_profile,
               **run.metrics.snapshot(), "background_since_previous_summary": run.owner.pending.snapshot(reset=True),
               "background_inflight": inflight}
    try:
        with _PRINT_LOCK:
            print(json.dumps(summary, separators=(",", ":")), flush=True)
    except Exception:
        pass


class _Timing:
    def __init__(self, name, is_stage=False):
        self.name, self.is_stage = name, is_stage

    def __enter__(self):
        self.run = _CURRENT.get()
        self.started = perf_counter()
        self.active_id = None
        if self.run is not None and self.run.background and not self.is_stage:
            with self.run.owner.lock:
                self.run.owner.sequence += 1
                self.active_id = self.run.owner.sequence
                self.run.owner.active[self.active_id] = self.name, self.started
        return self

    def __exit__(self, *unused):
        if self.run is not None:
            self.run.metrics.timing(self.name, perf_counter() - self.started, self.is_stage)
            if self.active_id is not None:
                with self.run.owner.lock:
                    self.run.owner.active.pop(self.active_id, None)
        return False


def stage(name):
    return _Timing(name, is_stage=True) if ENABLED else _NOTHING


def operation(name):
    if not ENABLED:
        return _identity

    def decorate(function):
        if getattr(function, "_corvium_timed", False):
            return function
        @wraps(function)
        def timed(*args, **kwargs):
            with _Timing(name):
                return function(*args, **kwargs)
        timed._corvium_timed = True
        return timed
    return decorate


def stage_function(name):
    if not ENABLED:
        return _identity

    def decorate(function):
        @wraps(function)
        def timed(*args, **kwargs):
            with stage(name):
                return function(*args, **kwargs)
        return timed
    return decorate


def entry(name):
    if not ENABLED:
        return _identity

    def decorate(function):
        @wraps(function)
        def entered(*args, **kwargs):
            _ensure_file_profile()
            run = _CURRENT.get()
            owns = run is None or run.finished or run.pending_callback
            if run is None or run.finished:
                run = _Run(_owner(), name)
                _CURRENT.set(run)
            elif run.pending_callback:
                run.kind, run.pending_callback = name, False
            try:
                with stage(name):
                    return function(*args, **kwargs)
            finally:
                if owns:
                    _finish(run)
        return entered
    return decorate


def callback(function):
    if not ENABLED:
        return function

    @wraps(function)
    def entered(*args, **kwargs):
        _ensure_file_profile()
        run = _CURRENT.get()
        if run is None or run.finished:
            run = _Run(_owner(), "callback")
            run.pending_callback = True
            _CURRENT.set(run)
        with stage("widget callback"):
            return function(*args, **kwargs)
    return entered


def worker(function):
    if not ENABLED:
        return function
    source = _CURRENT.get()
    if source is None:
        return function
    owner = source.owner

    @wraps(function)
    def entered(*args, **kwargs):
        previous = sys.getprofile()
        _ensure_file_profile()
        token = _CURRENT.set(_Run(owner, "background", background=True))
        try:
            with _Timing("job.worker"):
                return function(*args, **kwargs)
        finally:
            _CURRENT.reset(token)
            if previous is None and sys.getprofile() is _file_profile:
                sys.setprofile(None)
    return entered


def _flag(name):
    run = _CURRENT.get()
    if run is not None:
        with run.metrics.lock:
            setattr(run.metrics, name, True)


def _flagged(function, label, flag):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if flag != "setup" or function.__globals__.get("initialization_needed", lambda: True)():
            _flag(flag)
        with _Timing(label):
            return function(*args, **kwargs)
    return wrapped


def _record_sql(sql, seconds, count=1):
    run = _CURRENT.get()
    if run is not None:
        run.metrics.sql(sql, seconds, count)


def _sql_runner(function):
    @wraps(function)
    def wrapped(raw, dialect, sql, params=()):
        token = _SQL_DEPTH.set(_SQL_DEPTH.get() + 1)
        started = perf_counter()
        try:
            return function(raw, dialect, sql, params)
        finally:
            elapsed = perf_counter() - started
            _SQL_DEPTH.reset(token)
            _record_sql(sql, elapsed)
    return wrapped


def _sqlite_module(original):
    class TimedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if _SQL_DEPTH.get():
                return super().execute(sql, parameters)
            started = perf_counter()
            try:
                return super().execute(sql, parameters)
            finally:
                _record_sql(sql, perf_counter() - started)

        def executemany(self, sql, parameters):
            if _SQL_DEPTH.get():
                return super().executemany(sql, parameters)
            count = 0
            def counted():
                nonlocal count
                for values in parameters:
                    count += 1
                    yield values
            started = perf_counter()
            try:
                return super().executemany(sql, counted())
            finally:
                _record_sql(sql, perf_counter() - started, count)

        def commit(self):
            active, started = self.in_transaction, perf_counter()
            try:
                return super().commit()
            finally:
                if active:
                    _record_sql("COMMIT", perf_counter() - started)

        def rollback(self):
            active, started = self.in_transaction, perf_counter()
            try:
                return super().rollback()
            finally:
                if active:
                    _record_sql("ROLLBACK", perf_counter() - started)

    class Module:
        _corvium_profile_proxy = True
        def __getattr__(self, name):
            return getattr(original, name)

        def connect(self, *args, **kwargs):
            kwargs.setdefault("factory", TimedConnection)
            return original.connect(*args, **kwargs)
    return Module()


def install_db(namespace):
    if not ENABLED or namespace.get("_corvium_profile_installed"):
        return
    namespace["_corvium_profile_installed"] = True
    namespace["_run"] = _sql_runner(namespace["_run"])
    namespace["sqlite3"] = _sqlite_module(namespace["sqlite3"])
    for name, label in (("execute", "db.execute"), ("connect", "db.connect/setup"),
                        ("_checkout", "db.checkout.postgres"), ("_sqlite_open", "db.checkout.sqlite"),
                        ("get_file_bytes", "files_table.read")):
        namespace[name] = operation(label)(namespace[name])
    connection = namespace["_Connection"]
    connection.execute = operation("db.connection.execute")(connection.execute)
    for name, flag in (("init_db", "setup"), ("migrate_sqlite", "migrations"), ("migrate_postgres", "migrations")):
        namespace[name] = _flagged(namespace[name], "db." + name, flag)
    original = namespace["_pool"]
    @wraps(original)
    def pool():
        before = namespace["_POOL"]
        try:
            with _Timing("db.pool.obtain"):
                return original()
        finally:
            if before is None and namespace["_POOL"] is not None:
                _flag("pool")
    namespace["_pool"] = pool


def client_factory(original):
    if not ENABLED:
        return original
    @wraps(original)
    def create(*args, **kwargs):
        with _Timing("nebius.client.create"):
            client = original(*args, **kwargs)
        for resource, method, name in (
            (client.chat.completions, "create", "nebius.chat.completions.create"),
            (client.embeddings, "create", "nebius.embeddings.create"),
            (client.models, "list", "nebius.models.list"),
            (client.images, "generate", "nebius.images.generate"),
        ):
            setattr(resource, method, operation(name)(getattr(resource, method)))
        return client
    return create


def install_app(namespace):
    if not ENABLED:
        return
    for name, label in (("show_sidebar_header", "sidebar.header"), ("show_manage_chats", "sidebar.manage_chats"),
                        ("show_appearance", "sidebar.appearance"), ("show_empty_chat", "chat history render"),
                        ("show_sidebar_bridge", "sidebar.bridge"), ("show_rail", "sidebar.rail"),
                        ("load_logo", "branding/logo"),
                        ("show_notes_view", "notes view"), ("show_plan_view", "plan view"),
                        ("show_sources_panel", "sources panel")):
        namespace[name] = stage_function(label)(namespace[name])
    for module, name, label in (
        (namespace["flow"], "initialize_render", "startup/migrations/setup"),
        (namespace["flow"], "initialize_chat_selection", "sidebar.chat_selector.setup"),
        (namespace["flow"], "live_answer_job", "job polling logic"),
        (namespace["themes"], "initialize_appearance", "theme and CSS build"),
        (namespace["themes"], "styles", "theme and CSS build"),
    ):
        function = getattr(module, name)
        if not getattr(function, "_corvium_stage", False):
            wrapped = stage_function(label)(function)
            wrapped._corvium_stage = True
            setattr(module, name, wrapped)
    exports = namespace["note_exports"]
    if not getattr(exports.download_data, "_corvium_deferred", False):
        original_download = exports.download_data
        @wraps(original_download)
        def deferred(*args, **kwargs):
            return worker(original_download(*args, **kwargs))
        deferred._corvium_deferred = True
        exports.download_data = deferred
    original = namespace["render_section"]
    @wraps(original)
    def section(section_name, *args, **kwargs):
        label = section_name if section_name in {"study_plan", "tools", "library", "search", "notes", "bookmarks", "memory", "tips", "pinned"} else "other"
        with stage(("dialog_section." if kwargs.get("in_dialog") else "sidebar.") + label):
            return original(section_name, *args, **kwargs)
    namespace["render_section"] = section
    original_message = namespace["show_chat_message"]
    @wraps(original_message)
    def message(*args, **kwargs):
        with stage("pinned.message render" if kwargs.get("scope") == "pinned" else "chat history render"):
            return original_message(*args, **kwargs)
    namespace["show_chat_message"] = message
    st = namespace["st"]
    if not getattr(st.chat_input, "_corvium_stage", False):
        wrapped = stage_function("chat input area")(st.chat_input)
        wrapped._corvium_stage = True
        st.chat_input = wrapped

