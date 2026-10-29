from pathlib import Path

from .db import database_session


def _name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Name must not be empty.")
    return value.strip()


def _document(connection, document_id):
    row = connection.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        raise ValueError("Document does not exist.")
    return row


def _folder(connection, folder_id):
    if connection.execute("SELECT 1 FROM folders WHERE id = ?", (folder_id,)).fetchone() is None:
        raise ValueError("Folder does not exist.")


def _ordered_ids(connection, folder_id):
    return [row["id"] for row in connection.execute(
        "SELECT id FROM documents WHERE (folder_id = ? OR (folder_id IS NULL AND CAST(? AS BIGINT) IS NULL)) ORDER BY position, id", (folder_id, folder_id)
    )]


def _set_positions(connection, ids):
    connection.executemany("UPDATE documents SET position = ? WHERE id = ?", enumerate(ids))


def create_folder(name):
    name = _name(name)
    with database_session(write=True) as connection:
        position = connection.execute("SELECT COALESCE(MAX(position), -1) + 1 AS position FROM folders").fetchone()["position"]
        return connection.execute(
            'INSERT INTO folders(name, position) VALUES (?, ?) RETURNING id', (name, position)
        ).fetchone()["id"]


def rename_folder(folder_id, name):
    name = _name(name)
    with database_session(write=True) as connection:
        _folder(connection, folder_id)
        connection.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))


def delete_folder(folder_id):
    with database_session(write=True) as connection:
        _folder(connection, folder_id)
        ids = _ordered_ids(connection, None) + _ordered_ids(connection, folder_id)
        connection.execute("UPDATE documents SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
        _set_positions(connection, ids)
        connection.execute("DELETE FROM folders WHERE id = ?", (folder_id,))


def rename_document(document_id, display_name):
    display_name = _name(display_name)
    with database_session(write=True) as connection:
        _document(connection, document_id)
        connection.execute("UPDATE documents SET display_name = ? WHERE id = ?", (display_name, document_id))


def move_document(document_id, folder_id=None):
    with database_session(write=True) as connection:
        document = _document(connection, document_id)
        if folder_id is not None:
            _folder(connection, folder_id)
        if document["folder_id"] == folder_id:
            return
        position = connection.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS position FROM documents WHERE (folder_id = ? OR (folder_id IS NULL AND CAST(? AS BIGINT) IS NULL))", (folder_id, folder_id)
        ).fetchone()["position"]
        connection.execute(
            "UPDATE documents SET folder_id = ?, position = ? WHERE id = ?",
            (folder_id, position, document_id),
        )
        _set_positions(connection, _ordered_ids(connection, document["folder_id"]))


def reorder_document(document_id, direction):
    if direction not in {"up", "down"}:
        raise ValueError("Direction must be up or down.")
    with database_session(write=True) as connection:
        document = _document(connection, document_id)
        ids = _ordered_ids(connection, document["folder_id"])
        index = ids.index(document_id)
        target = index + (-1 if direction == "up" else 1)
        if 0 <= target < len(ids):
            ids[index], ids[target] = ids[target], ids[index]
        _set_positions(connection, ids)


def list_library():
    """Return folder groups, unfiled documents, and images without duplication."""
    with database_session() as connection:
        folders = [dict(row, documents=[]) for row in connection.execute(
            "SELECT * FROM folders ORDER BY position, id"
        )]
        documents = [dict(row) for row in connection.execute(
            "SELECT * FROM documents ORDER BY position, id"
        )]
    by_folder = {folder["id"]: folder["documents"] for folder in folders}
    unfiled, images = [], []
    for document in documents:
        if document["kind"] == "image":
            images.append(document)
        elif document["folder_id"] is None:
            unfiled.append(document)
        else:
            by_folder[document["folder_id"]].append(document)
    return {"folders": folders, "unfiled": unfiled, "images": images}


def list_documents():
    with database_session() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM documents ORDER BY position, id")]


def group_images_by_type(images):
    groups = {}
    for document in images:
        kind = Path(document["name"]).suffix.lstrip(".").upper() or "Other"
        if kind == "JPEG":
            kind = "JPG"
        groups.setdefault(kind, []).append(document)
    order = {"PNG": 0, "JPG": 1, "WEBP": 2}
    return dict(sorted(groups.items(), key=lambda item: (order.get(item[0], 3), item[0])))
