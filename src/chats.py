from datetime import datetime, timezone
import json
import re

if __package__:
    from .db import database_session
else:
    from db import database_session


def _now():
    return datetime.now(timezone.utc).isoformat()


def _title(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Chat title must not be empty.")
    return " ".join(value.split())[:40]


def _chat(connection, chat_id):
    row = connection.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if row is None:
        raise ValueError("Chat does not exist.")
    return row


def create_chat(title="New chat"):
    title = _title(title)
    now = _now()
    with database_session(write=True) as connection:
        return connection.execute(
            "INSERT INTO chats(title, created_at, updated_at) VALUES (?, ?, ?)", (title, now, now)
        ).lastrowid


def list_chats():
    with database_session() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM chats ORDER BY pinned DESC, "
            "CASE WHEN pinned != 0 THEN pinned_at END ASC, "
            "CASE WHEN pinned != 0 THEN id END ASC, updated_at DESC, id DESC"
        )]


def rename_chat(chat_id, title):
    title = _title(title)
    with database_session(write=True) as connection:
        _chat(connection, chat_id)
        connection.execute("UPDATE chats SET title = ?, updated_at = ? WHERE id = ?", (title, _now(), chat_id))


def delete_chat(chat_id):
    with database_session(write=True) as connection:
        _chat(connection, chat_id)
        connection.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        connection.execute("DELETE FROM chats WHERE id = ?", (chat_id,))


def add_message(chat_id, role, content, sources=None, error=False, kind="text"):
    if role not in {"user", "assistant", "system"}:
        raise ValueError("Invalid message role.")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Message must not be empty.")
    if kind not in {"text", "diagram", "plan", "quiz"}:
        raise ValueError("Invalid message kind.")
    now = _now()
    with database_session(write=True) as connection:
        chat = _chat(connection, chat_id)
        first_question = role == "user" and connection.execute(
            "SELECT 1 FROM messages WHERE chat_id = ? AND role = 'user' LIMIT 1", (chat_id,)
        ).fetchone() is None
        title = _title(content) if first_question else chat["title"]
        cursor = connection.execute(
            "INSERT INTO messages(session_id, chat_id, role, content, created_at, sources, error, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(chat_id), chat_id, role, content, now, json.dumps(sources or []), int(error), kind),
        )
        connection.execute("UPDATE chats SET title = ?, updated_at = ? WHERE id = ?", (title, now, chat_id))
        return cursor.lastrowid


def list_messages(chat_id):
    with database_session() as connection:
        _chat(connection, chat_id)
        messages = [dict(row) for row in connection.execute(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,)
        )]
    for message in messages:
        message["sources"] = json.loads(message["sources"])
        message["error"] = bool(message["error"])
    return messages


def initial_chat_id(chats=None):
    chats = list_chats() if chats is None else chats
    latest = most_recent_chat_id(chats)
    if latest is not None and not list_messages(latest):
        return latest
    return create_chat()


def get_message(chat_id, message_id):
    with database_session() as connection:
        _chat(connection, chat_id)
        row = connection.execute("SELECT * FROM messages WHERE chat_id = ? AND id = ?", (chat_id, message_id)).fetchone()
    if row is None:
        raise ValueError("Message does not exist in this chat.")
    message = dict(row)
    message["sources"] = json.loads(message["sources"])
    message["error"] = bool(message["error"])
    return message


def most_recent_chat_id(chats=None):
    chats = list_chats() if chats is None else chats
    return max(chats, key=lambda chat: (chat["updated_at"], chat["id"]))["id"] if chats else None


def _set_pinned(table, item_id, pinned):
    if type(pinned) not in {bool, int} or pinned not in (False, True):
        raise ValueError("Pinned must be true or false.")
    with database_session(write=True) as connection:
        row = connection.execute(f"SELECT pinned, pinned_at FROM {table} WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ValueError("Chat does not exist." if table == "chats" else "Message does not exist.")
        pinned_at = (row["pinned_at"] if row["pinned"] else None) if pinned else None
        if pinned and pinned_at is None:
            pinned_at = _now()
        connection.execute(f"UPDATE {table} SET pinned = ?, pinned_at = ? WHERE id = ?", (int(pinned), pinned_at, item_id))


def set_chat_pinned(chat_id, pinned=True):
    _set_pinned("chats", chat_id, pinned)


def set_message_pinned(message_id, pinned=True):
    _set_pinned("messages", message_id, pinned)


def list_pinned_messages():
    with database_session() as connection:
        messages = [dict(row) for row in connection.execute(
            "SELECT m.*, c.title AS chat_title FROM messages m JOIN chats c ON c.id = m.chat_id "
            "WHERE m.pinned != 0 ORDER BY m.pinned_at ASC, m.id ASC"
        )]
    for message in messages:
        message["sources"] = json.loads(message["sources"])
        message["error"] = bool(message["error"])
    return messages


def chat_label(chat):
    return ("📌 " if chat.get("pinned") else "") + chat["title"]


def pinned_message_label(message):
    text = " ".join(message["content"].split())
    preview = text[:80] + ("…" if len(text) > 80 else "")
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", preview)
