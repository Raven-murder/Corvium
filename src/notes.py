from datetime import datetime, timezone

from .db import database_session

MAX_NOTES = 500
MAX_BOOKMARKS = 500
MAX_TITLE = 120
MAX_BODY = 20_000


class NotesError(ValueError):
    public_message = True


def _now():
    return datetime.now(timezone.utc).isoformat()


def _title(value):
    if not isinstance(value, str) or not value.strip():
        raise NotesError("Enter a note title.")
    value = value.strip()
    if len(value) > MAX_TITLE:
        raise NotesError("Note titles can contain at most 120 characters.")
    return value


def _body(value):
    if not isinstance(value, str):
        raise NotesError("Enter note text.")
    if len(value) > MAX_BODY:
        raise NotesError("Notes can contain at most 20,000 characters.")
    return value


def _document(connection, doc_id):
    if type(doc_id) is not int or doc_id < 1:
        raise NotesError("Choose a valid document.")
    row = connection.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        raise NotesError("This document is no longer available.")
    return dict(row)


def _note(connection, note_id):
    row = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise NotesError("This note is no longer available.")
    return dict(row)


def create_note(title, body, doc_id=None):
    title, body = _title(title), _body(body)
    now = _now()
    with database_session(write=True) as connection:
        if doc_id is not None:
            _document(connection, doc_id)
        if connection.execute("SELECT COUNT(*) AS count FROM notes").fetchone()["count"] >= MAX_NOTES:
            raise NotesError("Your notes are full. Delete a note before adding another.")
        return connection.execute(
            'INSERT INTO notes(title, body, doc_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?) RETURNING id',
            (title, body, doc_id, now, now),
        ).fetchone()["id"]


def update_note(note_id, title, body):
    title, body = _title(title), _body(body)
    with database_session(write=True) as connection:
        _note(connection, note_id)
        connection.execute("UPDATE notes SET title = ?, body = ?, updated_at = ? WHERE id = ?",
                           (title, body, _now(), note_id))


def delete_note(note_id):
    with database_session(write=True) as connection:
        _note(connection, note_id)
        connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))


def list_notes(query=""):
    if not isinstance(query, str):
        raise NotesError("Enter text to filter notes.")
    with database_session() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT n.*, d.display_name AS doc_name FROM notes n LEFT JOIN documents d ON d.id = n.doc_id "
            "ORDER BY n.pinned DESC, n.updated_at DESC, n.id DESC"
        )]
    query = query.strip().casefold()
    return [row for row in rows if not query or query in (row["title"] + "\n" + row["body"]).casefold()]


def pin_note(note_id, pinned=True):
    if type(pinned) not in {bool, int} or pinned not in (False, True):
        raise NotesError("Pinned must be true or false.")
    with database_session(write=True) as connection:
        _note(connection, note_id)
        connection.execute("UPDATE notes SET pinned = ? WHERE id = ?", (int(pinned), note_id))


def _bookmark(connection, bookmark_id):
    row = connection.execute("SELECT * FROM bookmarks WHERE id = ?", (bookmark_id,)).fetchone()
    if row is None:
        raise NotesError("This bookmark is no longer available.")
    return dict(row)


def _locator(doc_id, locator):
    from .viewer import load_document, normalize_locator

    if not isinstance(locator, (str, dict)) and type(locator) is not int:
        raise NotesError("Choose a page, slide or section to bookmark.")
    if isinstance(locator, str) and not locator.strip():
        raise NotesError("Choose a page, slide or section to bookmark.")
    data = load_document(doc_id)
    normalized = normalize_locator(data, locator)
    kind = data["document"]["kind"]
    return str(normalized["page"]) if kind in {"pdf", "pptx", "image"} else normalized["section"] or str(normalized["page"])


def locator_label(kind, locator):
    if locator.isdigit():
        return ("Slide " if kind == "pptx" else "Page ") + locator
    return locator


def _label(value, default=None):
    if not isinstance(value, str):
        raise NotesError("Enter a bookmark label.")
    value = value.strip() or default
    if not value:
        raise NotesError("Enter a bookmark label.")
    if len(value) > MAX_TITLE:
        raise NotesError("Bookmark labels can contain at most 120 characters.")
    return value


def add_bookmark(doc_id, locator, label=""):
    locator = _locator(doc_id, locator)
    now = _now()
    with database_session(write=True) as connection:
        document = _document(connection, doc_id)
        existing = connection.execute("SELECT id FROM bookmarks WHERE doc_id = ? AND locator = ? ORDER BY id LIMIT 1",
                                      (doc_id, locator)).fetchone()
        if existing is not None:
            return existing["id"]
        label = _label(label, locator_label(document["kind"], locator)[:MAX_TITLE])
        if connection.execute("SELECT COUNT(*) AS count FROM bookmarks").fetchone()["count"] >= MAX_BOOKMARKS:
            raise NotesError("Your bookmarks are full. Delete a bookmark before adding another.")
        return connection.execute('INSERT INTO bookmarks(doc_id, locator, label, created_at) VALUES (?, ?, ?, ?) RETURNING id',
                                  (doc_id, locator, label, now)).fetchone()["id"]


def rename_bookmark(bookmark_id, label):
    label = _label(label)
    with database_session(write=True) as connection:
        _bookmark(connection, bookmark_id)
        connection.execute("UPDATE bookmarks SET label = ? WHERE id = ?", (label, bookmark_id))


def delete_bookmark(bookmark_id):
    with database_session(write=True) as connection:
        _bookmark(connection, bookmark_id)
        connection.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))


def list_bookmarks(doc_id=None):
    with database_session() as connection:
        if doc_id is not None:
            _document(connection, doc_id)
        return [dict(row) for row in connection.execute(
            "SELECT b.*, d.display_name AS doc_name, d.kind AS doc_kind FROM bookmarks b "
            "JOIN documents d ON d.id = b.doc_id "
            + ("WHERE b.doc_id = ? " if doc_id is not None else "")
            + "ORDER BY lower(d.display_name), d.id, b.created_at, b.id",
            (doc_id,) if doc_id is not None else (),
        )]


def current_bookmark(doc_id, locator):
    locator = _locator(doc_id, locator)
    return next((item for item in list_bookmarks(doc_id) if item["locator"] == locator), None)


def save_answer_note(chat_id, message_id):
    from .chats import get_message
    from .sources import group_answer_sources

    message = get_message(chat_id, message_id)
    if message["role"] != "assistant":
        raise NotesError("Only assistant answers can be saved with this control.")
    groups = group_answer_sources(message["sources"], message["content"])
    doc_id = next((group["doc_id"] for group in groups if group["doc_id"] is not None), None)
    return create_note(message["content"][:50], message["content"], doc_id)
