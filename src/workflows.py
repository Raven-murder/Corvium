import hashlib
from pathlib import Path

from .config import safe_error
from .workspaces import get_storage, ensure_workspace, cleanup_workspaces
from .db import init_db, delete_document
from .chats import create_chat, list_chats, rename_chat, delete_chat, initial_chat_id
from .library import create_folder, rename_folder, delete_folder, list_documents, rename_document, move_document
from .memory import add_memory, update_memory_text, delete_memory, clear_memories
from .diagrams import generate_diagram
from .actions import generate_study_plan, generate_quiz, grade_quiz
from .ingest import MAX_FILE_BYTES, ingest_file, get_ingest_status
from .demo import seed_demo, reset_demo


def initialize_chat_selection(state, chats):
    if not chats or not state.get("selected_chat_id"):
        state["selected_chat_id"] = initial_chat_id(chats)
        chats = list_chats()
    if state.get("selected_chat_id") not in {chat["id"] for chat in chats}:
        state["selected_chat_id"] = chats[0]["id"]
    return chats


def rename_chat_from_input(state, chat_id, key, previous_title):
    title = " ".join(state.get(key, "").split())[:40]
    if not title or title == previous_title:
        return
    try:
        rename_chat(chat_id, title)
    except Exception as error:
        state["sidebar_error"] = safe_error(error)


def delete_chat_from_sidebar(state, chat_id, confirmed=False):
    if not confirmed and state.get("chat_delete_pending") != chat_id:
        state["chat_delete_pending"] = chat_id
        return
    try:
        delete_chat(chat_id)
        if state.get("selected_chat_id") == chat_id:
            remaining = list_chats()
            state["selected_chat_id"] = remaining[0]["id"] if remaining else create_chat()
    except Exception as error:
        state["sidebar_error"] = safe_error(error)
        return False
    else:
        state.pop("chat_delete_pending", None)
        return True


def create_folder_from_input(state):
    name = state.get("new_folder_name", "").strip()
    if not name:
        return
    try:
        create_folder(name)
    except Exception as error:
        state["sidebar_error"] = safe_error(error)
    else:
        state["new_folder_name"] = ""


def rename_folder_from_input(state, folder_id, key, previous_name):
    name = state.get(key, "").strip()
    if not name or name == previous_name:
        return
    try:
        rename_folder(folder_id, name)
    except Exception as error:
        state["sidebar_error"] = safe_error(error)


def delete_folder_from_sidebar(state, folder_id, confirmed=False):
    key = f"folder_delete_pending_{folder_id}"
    if not confirmed and not state.get(key):
        state[key] = True
        return
    try:
        delete_folder(folder_id)
    except Exception as error:
        state["sidebar_error"] = safe_error(error)
        return False
    else:
        state.pop(key, None)
        return True


def delete_document_from_sidebar(state, document, document_key, confirmed=False):
    key = f"delete_pending_{document_key}"
    if not confirmed and not state.get(key):
        state[key] = True
        return
    try:
        delete_document(document["name"])
        state["processed_files"].pop(document["name"], None)
        state["uploader_version"] = state.get("uploader_version", 0) + 1
    except Exception as error:
        state["sidebar_error"] = safe_error(error)
        return False
    else:
        state.pop(key, None)
        return True


def initialize_library():
    storage = get_storage()
    ensure_workspace(storage)
    cleanup_workspaces(exclude=(storage.workspace_id,))
    init_db()
    return []


def reset_demo_from_sidebar(state):
    version = state.get("uploader_version", 0) + 1
    workspace_id = state.get("workspace_id")
    reset_demo()
    state.clear()
    if workspace_id:
        state["workspace_id"] = workspace_id
    state["uploader_version"] = version


def process_uploads(uploads, state):
    events = []
    processed = state.setdefault("processed_files", {})
    for upload in uploads or []:
        name = Path(upload.name.replace("\\", "/")).name
        if not name or name in {".", ".."}:
            events.append({"kind": "error", "message": "This upload has an invalid filename."})
            continue
        fingerprint = None
        try:
            if upload.size > MAX_FILE_BYTES:
                events.append({"kind": "error", "message": f"{name}: This file exceeds the 25 MB limit. Please upload a smaller file."})
                continue
            contents = upload.getvalue()
            fingerprint = hashlib.sha256(contents).hexdigest()
            previous = processed.get(name)
            if previous and previous.get("fingerprint") == fingerprint:
                continue
            folder = get_storage().uploads_dir
            folder.mkdir(parents=True, exist_ok=True)
            destination = folder / name
            destination.write_bytes(contents)
            count = ingest_file(destination)
            status = get_ingest_status(name)
            processed[name] = {"fingerprint": fingerprint, "count": count, "error": None}
            events.append({"kind": "success" if status == "indexed" else "warning",
                           "message": f"{name}: {status} ({count} chunks)."})
        except Exception as error:
            processed[name] = {"fingerprint": fingerprint, "error": safe_error(error)}
    return events


def retry_failed_uploads(state):
    for name, entry in list(state.get("processed_files", {}).items()):
        if entry.get("error"):
            del state["processed_files"][name]


def add_focus_topic(state):
    topic = state.get("new_focus_topic", "").strip()
    if not topic:
        return
    state["custom_focus_topics"] = list(dict.fromkeys(state.get("custom_focus_topics", []) + [topic]))
    state["plan_focus_topics"] = list(dict.fromkeys(state.get("plan_focus_topics", []) + [topic]))
    state["new_focus_topic"] = ""


def edit_memory_from_input(state, memory_id, key):
    text = state.get(key, "").strip()
    if text:
        update_memory_text(memory_id, text)


def add_memory_from_input(state):
    add_memory(state.get("new_memory_text", ""), state.get("new_memory_kind", "fact"))
    state["new_memory_text"] = ""


def delete_memory_from_sidebar(state, memory_id):
    delete_memory(memory_id)


def clear_memories_from_sidebar(state):
    clear_memories()
    state.pop("confirm_clear_memories", None)


def run_tool(state, kind, parameters):
    if kind == "plan":
        state["study_plan"] = generate_study_plan(**parameters)
    elif kind == "quiz":
        state["quiz"] = generate_quiz(**parameters)
        state["quiz_version"] = state.get("quiz_version", 0) + 1
        state.pop("quiz_answers", None)
    elif kind == "diagram":
        state["diagram_result"] = generate_diagram(**parameters)
    else:
        raise ValueError("Unknown tool.")
    state["active_tool"] = kind


def close_tool_result(state):
    state.pop("active_tool", None)


def check_quiz_answers(state, answers):
    if any(answer is None for answer in answers):
        raise ValueError("Select an answer for every question before checking.")
    grade_quiz(state["quiz"], answers)
    state["quiz_answers"] = answers


def new_chat(state):
    state["selected_chat_id"] = create_chat()


def edit_document(state, document_id, display_name, folder_id):
    rename_document(document_id, display_name)
    move_document(document_id, folder_id)


def request_destructive_action(state, kind, name, item=None):
    if kind not in {"document", "folder", "memory", "memories", "demo", "chat"}:
        raise ValueError("Unknown destructive action.")
    token = state.get("confirmation_version", 0) + 1
    state["confirmation_version"] = token
    state["destructive_confirmation"] = {"kind": kind, "name": name, "item": item, "token": token}


def cancel_destructive_action(state, token):
    request = state.get("destructive_confirmation")
    if request and request["token"] == token:
        state.pop("destructive_confirmation", None)


def confirm_destructive_action(state, token):
    request = state.get("destructive_confirmation")
    if not request or request["token"] != token:
        return False
    kind, item = request["kind"], request["item"]
    if kind == "document":
        result = delete_document_from_sidebar(state, item["document"], item["document_key"], confirmed=True)
    elif kind == "folder":
        result = delete_folder_from_sidebar(state, item, confirmed=True)
    elif kind == "chat":
        result = delete_chat_from_sidebar(state, item, confirmed=True)
    elif kind == "memory":
        result = delete_memory_from_sidebar(state, item)
    elif kind == "memories":
        result = clear_memories_from_sidebar(state)
    elif kind == "demo":
        result = reset_demo_from_sidebar(state)
    else:
        raise ValueError("Unknown destructive action.")
    if result is False:
        return False
    state.pop("destructive_confirmation", None)
    return True


def queue_starter(state, kind):
    if kind not in {"summary", "plan", "quiz"}:
        raise ValueError("Unknown starter.")
    state["starter_action"] = {"kind": kind, "chat_id": state.get("selected_chat_id")}


def consume_starter(state):
    request = state.pop("starter_action", None)
    if not request or request["chat_id"] != state.get("selected_chat_id"):
        return None, None
    if request["kind"] == "summary":
        return "Summarize my notes", None
    if request["kind"] in {"plan", "quiz"}:
        return None, (request["kind"], {})
    return None, None
