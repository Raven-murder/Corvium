from copy import deepcopy
from dataclasses import dataclass, field
from threading import Event, Lock, RLock, Thread
import json
import time
from uuid import uuid4

from . import config, perf
from .actions import generate_quiz, generate_study_plan
from .chats import add_message, get_message, list_messages
from .db import database_session
from .plans import create_plan, get_plan, pin_plan
from .diagrams import diagram_request_kind, generate_diagram, parse_diagram_message
from .qa import answer_question
from .usage import UsageLimitError
from .workspaces import database_identity, validate_workspace_id, workspace_context, workspace_storage

MAX_SECONDS = 180
RETENTION_SECONDS = 600
_VERSION_LOCK = RLock()
_DATA_VERSIONS = {}


class JobBusyError(RuntimeError):
    public_message = True


class _Interrupted(BaseException):
    """Bypass optional memory and retry handlers when stopping a job."""

    def __init__(self, reason):
        self.reason = reason


@dataclass
class _Job:
    id: str
    workspace_id: str
    chat_id: int
    kind: str
    user_message_id: int | None
    user_created_at: str
    database: tuple
    language: str
    chat_created_at: str | None = None
    regenerate_plan_id: int | None = None
    final_plan_id: int | None = None
    status: str = "running"
    partial_text: str = ""
    final_message_id: int | None = None
    error_message: str | None = None
    cancel_flag: Event = field(default_factory=Event, repr=False)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    started: float = field(default_factory=time.monotonic, repr=False)
    sources: list = field(default_factory=list, repr=False)
    finalized: bool = False
    finish_lock: Lock = field(default_factory=Lock, repr=False)


class JobManager:
    def __init__(self):
        self.lock = RLock()
        self.jobs = {}
        self.workers = {}
        self.monitor = None

    def start(self, workspace_id, chat_id, question, *, diagram_parameters=None, language="json",
              tool_kind=None, tool_parameters=None, regenerate_plan_id=None):
        workspace_id = validate_workspace_id(workspace_id)
        if type(chat_id) is not int or chat_id < 1 or not isinstance(question, str) or not question.strip():
            raise ValueError("Enter a question in a valid chat.")
        if language != "json":
            raise ValueError("Choose a supported diagram format.")
        parameters = None if diagram_parameters is None else dict(diagram_parameters)
        if parameters is not None and parameters.get("doc_ids") is not None:
            parameters["doc_ids"] = list(parameters["doc_ids"])
        if tool_kind is not None:
            if tool_kind not in {"plan", "quiz"} or diagram_parameters is not None:
                raise ValueError("Choose a supported study tool.")
            parameters = deepcopy(tool_parameters or {})
        settings = config.capture_client_settings()
        with self.lock:
            worker = self.workers.get(workspace_id)
            if (worker is not None and worker.is_alive()) or any(
                job.workspace_id == workspace_id and (job.status == "running" or not job.finalized) for job in self.jobs.values()
            ):
                raise JobBusyError("A generation is already running or finishing in this workspace. Please wait or stop it before starting another.")
            with workspace_context(workspace_id) as storage:
                chat_created_at = None
                if tool_kind is None:
                    history = list_messages(chat_id)
                    message_id = add_message(chat_id, "user", question)
                    message_created_at = get_message(chat_id, message_id)["created_at"]
                else:
                    with database_session() as connection:
                        chat = connection.execute("SELECT created_at FROM chats WHERE id = ?", (chat_id,)).fetchone()
                    if chat is None:
                        raise ValueError("This chat is no longer available.")
                    chat_created_at = chat["created_at"]
                    history, message_id, message_created_at = [], None, ""
                    if regenerate_plan_id is not None:
                        if tool_kind != "plan":
                            raise ValueError("Only study plans can be regenerated.")
                        previous = get_plan(regenerate_plan_id)
                        parameters = dict(previous["body"].get("parameters", {}))
                        parameters["days"] = previous["days"]
                identity = database_identity(storage)
            job = _Job(uuid4().hex, workspace_id, chat_id,
                       tool_kind or ("diagram" if diagram_parameters is not None or diagram_request_kind(question) else "answer"),
                       message_id, message_created_at, identity, language,
                       chat_created_at=chat_created_at, regenerate_plan_id=regenerate_plan_id)
            self.jobs[job.id] = job
            worker = Thread(target=perf.worker(self._run) if perf.ENABLED else self._run, args=(job, question, history, parameters, settings), daemon=True)
            self.workers[workspace_id] = worker
            try:
                worker.start()
            except Exception:
                self._finish(job, "error", error="The answer could not be started. Please try again.")
            if self.monitor is None or not self.monitor.is_alive():
                self.monitor = Thread(target=self._watch, daemon=True)
                self.monitor.start()
            return self._snapshot(job)

    def _check(self, job):
        with self.lock:
            if job.status != "running":
                raise _Interrupted("finished")
            if job.cancel_flag.is_set():
                raise _Interrupted("cancelled")
        if time.monotonic() - job.started >= MAX_SECONDS:
            raise _Interrupted("timeout")
        try:
            if database_identity(workspace_storage(job.workspace_id)) != job.database:
                raise _Interrupted("unavailable")
        except OSError:
            raise _Interrupted("unavailable") from None

    def _append(self, job, text):
        self._check(job)
        with self.lock:
            if job.status == "running" and not job.cancel_flag.is_set():
                job.partial_text += text
                job.updated_at = time.time()

    def _reset(self, job):
        self._check(job)
        with self.lock:
            if job.status == "running" and not job.cancel_flag.is_set():
                job.partial_text = ""
                job.updated_at = time.time()

    def _sources(self, job, sources):
        self._check(job)
        with self.lock:
            if job.status == "running" and not job.cancel_flag.is_set():
                job.sources = [dict(source) for source in sources]

    def _run(self, job, question, history, parameters, settings):
        try:
            with workspace_context(job.workspace_id, job.database), config.client_context(
                settings, lambda: self._check(job), lambda: MAX_SECONDS - (time.monotonic() - job.started)
            ):
                self._check(job)
                callbacks = {"on_chunk": lambda text: self._append(job, text), "check_cancel": lambda: self._check(job),
                             "on_sources": lambda sources: self._sources(job, sources), "reset_partial": lambda: self._reset(job),
                             "language": job.language}
                if job.kind in {"plan", "quiz"}:
                    result = self._generate_tool(job, parameters)
                elif parameters is None:
                    result = answer_question(question, history=history, **callbacks)
                else:
                    result = generate_diagram(**parameters, **callbacks)
                self._check(job)
                if not isinstance(result, dict) or not isinstance(result.get("answer"), str) or not result["answer"].strip() or not isinstance(result.get("sources"), list):
                    raise ValueError("The answer was incomplete.")
                self._finish(job, "done", result=result)
        except _Interrupted as interruption:
            if interruption.reason == "cancelled":
                self._finish(job, "cancelled")
            elif interruption.reason == "timeout":
                self._finish(job, "error", error="This answer took too long. Please try again.")
            elif interruption.reason != "finished":
                self._finish(job, "error", error="This chat or workspace changed while the answer was running.")
        except Exception as error:
            message = str(error) if isinstance(error, (UsageLimitError, config.StorageError)) else "The answer could not be completed. Please try again."
            self._finish(job, "error", error=message)

    def _generate_tool(self, job, parameters):
        with database_session() as connection:
            memories = [dict(row) for row in connection.execute(
                "SELECT id, text, kind, created_at FROM memories ORDER BY id DESC"
            )]
        self._check(job)
        if job.kind == "plan":
            content = generate_study_plan(**{**parameters, "memories": memories})
            if content.startswith("No documents are indexed."):
                raise ValueError(content)
        else:
            content = json.dumps(generate_quiz(**{**parameters, "memories": memories}), ensure_ascii=False)
        return {"answer": content, "sources": [], "parameters": parameters}

    def _finish(self, job, status, *, result=None, error=None):
        with job.finish_lock:
            with self.lock:
                if job.status != "running":
                    return
                if status == "done" and job.cancel_flag.is_set():
                    status = "cancelled"
                job.status = status
                job.error_message = error
                job.finished_at = job.updated_at = time.time()
                if status != "done":
                    job.cancel_flag.set()
                partial, sources = job.partial_text, list(job.sources)
            kind = "text"
            if status == "done":
                content, sources = result["answer"], result["sources"]
                if job.kind == "diagram" and parse_diagram_message(content):
                    kind = "diagram"
                elif job.kind in {"plan", "quiz"}:
                    kind = job.kind
            elif status == "cancelled":
                content = partial.strip() or ("Stopped before a result was generated." if job.kind in {"plan", "quiz"}
                                             else "Stopped before an answer was generated.")
                if partial.strip() and job.kind == "diagram":
                    kind = "diagram"
                content += "\n\n*Stopped*"
            else:
                content, sources = error, []
            message_id = plan_id = None
            try:
                with workspace_context(job.workspace_id, job.database):
                    if job.user_message_id is not None:
                        user = get_message(job.chat_id, job.user_message_id)
                        if user["created_at"] != job.user_created_at or user["role"] != "user":
                            raise ValueError("The original question is no longer available.")
                    else:
                        with database_session() as connection:
                            chat = connection.execute("SELECT created_at FROM chats WHERE id = ?", (job.chat_id,)).fetchone()
                        if chat is None or chat["created_at"] != job.chat_created_at:
                            raise ValueError("The original chat is no longer available.")
                    if status == "done" and kind == "plan":
                        parameters = result["parameters"]
                        days = parameters.get("days", 7)
                        previous = get_plan(job.regenerate_plan_id) if job.regenerate_plan_id is not None else None
                        title = previous["title"] if previous else f"Study plan ({days} days)"
                        plan_id = create_plan(title, {"markdown": content, "parameters": parameters}, days)
                        if previous and previous["pinned"]:
                            pin_plan(plan_id)
                    message_id = add_message(job.chat_id, "assistant", content, sources=sources, kind=kind, error=status == "error")
            except Exception as error:
                with self.lock:
                    job.status = "error"
                    job.error_message = str(error) if isinstance(error, config.StorageError) else "The answer could not be saved because this chat or workspace is no longer available."
            finally:
                with self.lock:
                    job.final_message_id = message_id
                    job.final_plan_id = plan_id
                    if status == "done" and message_id is not None:
                        job.partial_text = content
                    job.finalized = True
                    job.updated_at = time.time()

    def _watch(self):
        while True:
            with self.lock:
                running = [job for job in self.jobs.values() if job.status == "running"]
            for job in running:
                try:
                    if time.monotonic() - job.started >= MAX_SECONDS:
                        self._finish(job, "error", error="This answer took too long. Please try again.")
                    elif job.cancel_flag.is_set():
                        self._finish(job, "cancelled")
                except Exception:
                    continue
            with self.lock:
                now = time.time()
                for job_id, job in list(self.jobs.items()):
                    if job.finalized and job.finished_at is not None and now - job.finished_at >= RETENTION_SECONDS:
                        del self.jobs[job_id]
                for workspace_id, worker in list(self.workers.items()):
                    if not worker.is_alive():
                        del self.workers[workspace_id]
            Event().wait(0.5)

    def _snapshot(self, job):
        return {name: getattr(job, name) for name in (
            "id", "workspace_id", "chat_id", "kind", "status", "partial_text", "final_message_id",
            "error_message", "created_at", "updated_at", "finished_at", "finalized", "language",
            "final_plan_id", "regenerate_plan_id"
        )} | {"cancel_requested": job.cancel_flag.is_set()}

    def latest(self, workspace_id, chat_id=None, *, running_only=False):
        from . import db
        workspace_id = validate_workspace_id(workspace_id)
        with self.lock:
            candidates = [job for job in self.jobs.values() if job.workspace_id == workspace_id
                          and (chat_id is None or job.chat_id == chat_id)
                          and (not running_only or job.status == "running" or not job.finalized)]
        if not candidates:
            return None
        if db.backend() == "postgres":
            identity = db.known_workspace_identity(workspace_id)
        else:
            try:
                identity = database_identity(workspace_storage(workspace_id))
            except OSError:
                return None
        with self.lock:
            matches = [job for job in candidates if identity is None or job.database == identity]
            return self._snapshot(max(matches, key=lambda job: job.started)) if matches else None

    def cancel(self, workspace_id, chat_id, job_id):
        workspace_id = validate_workspace_id(workspace_id)
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None and job.workspace_id == workspace_id and job.chat_id == chat_id and job.status == "running":
                job.cancel_flag.set()
                job.updated_at = time.time()
                return True
        return False


_MANAGER = JobManager()


def start_job(workspace_id, chat_id, question, **parameters):
    return _MANAGER.start(workspace_id, chat_id, question, **parameters)


def start_tool_job(workspace_id, chat_id, kind, parameters, *, regenerate_plan_id=None):
    return _MANAGER.start(workspace_id, chat_id, "Generate a " + kind, tool_kind=kind,
                          tool_parameters=parameters, regenerate_plan_id=regenerate_plan_id)


def latest_job(workspace_id, chat_id):
    return _MANAGER.latest(workspace_id, chat_id)


def cancel_job(workspace_id, chat_id, job_id):
    return _MANAGER.cancel(workspace_id, chat_id, job_id)


def running_job(workspace_id):
    return _MANAGER.latest(workspace_id, running_only=True)


def data_version(workspace_id):
    with _VERSION_LOCK:
        return _DATA_VERSIONS.get(workspace_id, 0)


def bump_data_version(workspace_id):
    if workspace_id is not None:
        with _VERSION_LOCK:
            _DATA_VERSIONS[workspace_id] = _DATA_VERSIONS.get(workspace_id, 0) + 1
