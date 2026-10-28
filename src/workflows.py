import hashlib
import json
from pathlib import Path

from .config import safe_error
from .workspaces import get_storage, ensure_workspace, cleanup_workspaces
from .db import init_db, delete_document
from .chats import create_chat, list_chats, rename_chat, delete_chat, initial_chat_id, add_message, list_messages, get_message, most_recent_chat_id, set_chat_pinned, set_message_pinned
from .library import create_folder, rename_folder, delete_folder, list_documents, rename_document, move_document
from .memory import add_memory, update_memory_text, delete_memory, clear_memories
from .diagrams import generate_diagram, parse_diagram_message
from .actions import generate_study_plan, generate_quiz, grade_quiz
from .plans import create_plan, list_plans, get_plan, rename_plan, pin_plan, delete_plan
from . import notes
from .ingest import MAX_FILE_BYTES, ingest_file, get_ingest_status
from .demo import seed_demo, reset_demo


def initialize_chat_selection(state, chats):
    if not chats or not state.get("selected_chat_id"):
        state["selected_chat_id"] = initial_chat_id(chats)
        chats = list_chats()
    if state.get("selected_chat_id") not in {chat["id"] for chat in chats}:
        state["selected_chat_id"] = most_recent_chat_id(chats)
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
            state["selected_chat_id"] = most_recent_chat_id(remaining) if remaining else create_chat()
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
    if kind not in {"plan", "quiz", "diagram"}:
        raise ValueError("Unknown tool.")
    chat_id = state.get("selected_chat_id")
    if chat_id is None:
        chat_id = initial_chat_id()
        state["selected_chat_id"] = chat_id
    list_messages(chat_id)
    saved_plan_id = None
    sources = []
    message_kind = kind
    if kind == "plan":
        content = generate_study_plan(**parameters)
        if content.startswith("No documents are indexed."):
            raise ValueError(content)
        days = parameters.get("days", 7)
        saved_plan_id = create_plan(f"Study plan ({days} days)", {"markdown": content, "parameters": parameters}, days)
    elif kind == "quiz":
        content = json.dumps(generate_quiz(**parameters), ensure_ascii=False)
    else:
        result = generate_diagram(**parameters)
        content, sources = result["answer"], result["sources"]
        if not result.get("code"):
            message_kind = "text"
    message_id = add_message(chat_id, "assistant", content, sources=sources, kind=message_kind)
    state["view"] = "chat"
    return {"message_id": message_id, "plan_id": saved_plan_id}


def quiz_for_message(message):
    if message.get("kind") != "quiz":
        raise ValueError("This message does not contain a quiz.")
    quiz = json.loads(message["content"])
    if not isinstance(quiz, list) or not quiz or any(not isinstance(item, dict) for item in quiz):
        raise ValueError("The saved quiz is invalid.")
    return quiz


def _quiz_state(state, key):
    if not isinstance(state.get(key), dict):
        state[key] = {}
    return state[key]


def quiz_selections(state, message):
    quiz = quiz_for_message(message)
    selections = _quiz_state(state, "quiz_selections")
    checked = _quiz_state(state, "quiz_answers")
    identities = _quiz_state(state, "quiz_identities")
    identity = hashlib.sha256(json.dumps((message["chat_id"], message["created_at"], message["content"])).encode("utf-8")).hexdigest()
    if identities.get(message["id"]) != identity or len(selections.get(message["id"], [])) != len(quiz):
        identities[message["id"]] = identity
        selections[message["id"]] = [None] * len(quiz)
        checked.pop(message["id"], None)
    return selections[message["id"]]


def choose_quiz_answer(state, message_id, question_index, widget_key, chat_id=None):
    message = get_message(state["selected_chat_id"] if chat_id is None else chat_id, message_id)
    selections = quiz_selections(state, message)
    value = state.get(widget_key)
    if type(question_index) is not int or not 0 <= question_index < len(selections):
        raise ValueError("Invalid quiz question.")
    if type(value) is not int or not 0 <= value < 4:
        raise ValueError("Select a quiz option.")
    selections[question_index] = value
    for scope in ("chat", "pinned"):
        key = quiz_choice_key(message, question_index + 1, scope)
        if key in state:
            state[key] = value
    _quiz_state(state, "quiz_answers").pop(message_id, None)


def check_quiz_answers(state, message_id, chat_id=None):
    message = get_message(state["selected_chat_id"] if chat_id is None else chat_id, message_id)
    quiz = quiz_for_message(message)
    answers = quiz_selections(state, message)
    if any(answer is None for answer in answers):
        raise ValueError("Select an answer for every question before checking.")
    grade_quiz(quiz, answers)
    _quiz_state(state, "quiz_answers")[message_id] = list(answers)


def new_chat(state):
    state["selected_chat_id"] = create_chat()
    state["view"] = "chat"


def edit_document(state, document_id, display_name, folder_id):
    rename_document(document_id, display_name)
    move_document(document_id, folder_id)


def request_destructive_action(state, kind, name, item=None):
    if kind not in {"document", "folder", "memory", "memories", "demo", "chat", "plan", "note", "bookmark"}:
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
    elif kind == "note":
        notes.delete_note(item)
        result = True
    elif kind == "bookmark":
        notes.delete_bookmark(item)
        result = True
    elif kind == "plan":
        delete_plan(item)
        if state.get("selected_plan_id") == item:
            state.pop("selected_plan_id", None)
        result = True
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


def open_diagram_view(state, content, message_key):
    diagram = parse_diagram_message(content)
    if diagram is None:
        raise ValueError("This message does not contain a valid diagram.")
    state["diagram_view"] = {**diagram, "message_key": message_key, "chat_id": state.get("selected_chat_id")}


def active_diagram_view(state):
    diagram = state.get("diagram_view")
    if diagram and diagram["chat_id"] != state.get("selected_chat_id"):
        close_diagram_view(state)
        return None
    return diagram


def close_diagram_view(state):
    state.pop("diagram_view", None)


def open_chat_view(state):
    state["view"] = "chat"


def open_plan_view(state):
    state["view"] = "plan"
    plans = list_plans()
    if plans:
        state["selected_plan_id"] = plans[0]["id"]
    else:
        state.pop("selected_plan_id", None)


def initialize_plan_selection(state, plans):
    if not plans:
        state.pop("selected_plan_id", None)
        return
    if state.get("selected_plan_id") not in {plan["id"] for plan in plans}:
        state["selected_plan_id"] = plans[0]["id"]


def rename_plan_from_input(state, plan_id, key, previous_title):
    title = " ".join(state.get(key, "").split())[:120]
    if title and title != previous_title:
        rename_plan(plan_id, title)


def open_tools(state):
    state["tools"] = True
    state["view"] = "chat"


def queue_plan_regeneration(state, plan_id):
    get_plan(plan_id)
    state["regenerate_plan_id"] = plan_id


def regenerate_plan(state, plan_id):
    plan = get_plan(plan_id)
    parameters = dict(plan["body"].get("parameters", {}))
    parameters["days"] = plan["days"]
    result = run_tool(state, "plan", parameters)
    rename_plan(result["plan_id"], plan["title"])
    if plan["pinned"]:
        pin_plan(result["plan_id"])
    state["selected_plan_id"] = result["plan_id"]
    state["view"] = "plan"
    return result


def set_plan_pinned(state, plan_id, pinned):
    pin_plan(plan_id, pinned)


def quiz_choice_key(message, number, scope="chat"):
    return f"quiz_choice_{scope}_{message['chat_id']}_{message['id']}_{number}"


def pin_chat_from_sidebar(state, chat_id, pinned):
    set_chat_pinned(chat_id, pinned)


def pin_message_from_view(state, chat_id, message_id, pinned):
    get_message(chat_id, message_id)
    set_message_pinned(message_id, pinned)


def open_pinned_message(state, chat_id, message_id):
    message = get_message(chat_id, message_id)
    if not message["pinned"]:
        raise ValueError("This message is no longer pinned.")
    state["pinned_message_view"] = {"chat_id": chat_id, "message_id": message_id,
                                    "created_at": message["created_at"], "workspace": str(get_storage().root)}
    for key in ("diagram_view", "viewer_request", "sources_panel"):
        state.pop(key, None)


def active_pinned_message(state):
    request = state.get("pinned_message_view")
    if not request:
        return None
    if request["workspace"] != str(get_storage().root):
        close_pinned_message(state)
        return None
    try:
        message = get_message(request["chat_id"], request["message_id"])
        if message["created_at"] != request["created_at"]:
            raise ValueError("Message has changed.")
    except ValueError:
        close_pinned_message(state)
        state["chat_notice"] = "This pinned message is no longer available."
        return None
    message["chat_title"] = next(chat["title"] for chat in list_chats() if chat["id"] == message["chat_id"])
    return message


def close_pinned_message(state):
    state.pop("pinned_message_view", None)


def go_to_pinned_chat(state):
    message = active_pinned_message(state)
    if message is not None:
        state["selected_chat_id"] = message["chat_id"]
        state["view"] = "chat"
    close_pinned_message(state)


def create_note_from_input(state):
    notes.create_note(state.get("new_note_title", ""), state.get("new_note_body", ""))
    state["new_note_title"], state["new_note_body"] = "", ""


def update_note_from_input(state, note_id, title_key, body_key):
    notes.update_note(note_id, state.get(title_key, ""), state.get(body_key, ""))


def pin_note_from_sidebar(state, note_id, pinned):
    notes.pin_note(note_id, pinned)


def save_answer_to_notes(state, chat_id, message_id):
    notes.save_answer_note(chat_id, message_id)
    state["note_saved_message"] = (chat_id, message_id)


def rename_bookmark_from_input(state, bookmark_id, key):
    notes.rename_bookmark(bookmark_id, state.get(key, ""))


def open_bookmark(state, bookmark_id):
    from .viewer import request_viewer

    bookmark = next((item for item in notes.list_bookmarks() if item["id"] == bookmark_id), None)
    if bookmark is None:
        raise notes.NotesError("This bookmark is no longer available.")
    request_viewer(state, bookmark["doc_id"], bookmark["locator"])
