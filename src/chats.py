from datetime import datetime, timezone
import json

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
        return [dict(row) for row in connection.execute("SELECT * FROM chats ORDER BY updated_at DESC, id DESC")]


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


def add_message(chat_id, role, content, sources=None, error=False):
    if role not in {"user", "assistant", "system"}:
        raise ValueError("Invalid message role.")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Message must not be empty.")
    now = _now()
    with database_session(write=True) as connection:
        chat = _chat(connection, chat_id)
        first_question = role == "user" and connection.execute(
            "SELECT 1 FROM messages WHERE chat_id = ? AND role = 'user' LIMIT 1", (chat_id,)
        ).fetchone() is None
        title = _title(content) if first_question else chat["title"]
        cursor = connection.execute(
            "INSERT INTO messages(session_id, chat_id, role, content, created_at, sources, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(chat_id), chat_id, role, content, now, json.dumps(sources or []), int(error)),
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
    if chats and not list_messages(chats[0]["id"]):
        return chats[0]["id"]
    return create_chat()
