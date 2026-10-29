"""Run disposable storage checks manually; never display database credentials."""

from pathlib import Path
import sys
from threading import Thread
import time
from uuid import uuid4


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from dotenv import load_dotenv
    load_dotenv(root / ".env", override=False)
    from src import config, db, workspaces, library, chats, notes, plans, memory, viewer
    import numpy as np

    ids = [uuid4().hex for _ in range(5)]
    fixtures = {}
    failed = []

    def check(label, action):
        try:
            action()
        except Exception:
            failed.append(label)
            print("FAIL " + label)
            return False
        print("PASS " + label)
        return True

    def require(value):
        if not value:
            raise AssertionError()

    def blocked(action):
        try:
            action()
        except ValueError:
            return
        raise AssertionError()

    def seed(workspace_id):
        db.create_workspace(workspace_id)
        with workspaces.workspace_context(workspace_id):
            folder = library.create_folder("Private subject")
            with db.transaction() as connection:
                document = connection.execute("INSERT INTO documents(name, uploaded_at, n_chunks, folder_id) VALUES (?, ?, 2, ?) RETURNING id",
                                              ("notes-" + workspace_id + ".md", "2026-01-01", folder)).fetchone()["id"]
                vector = db.embedding_to_blob(np.random.default_rng().normal(size=4096).astype(np.float32))
                for page in (1, 2):
                    connection.execute("INSERT INTO chunks(doc_id, page, section, text, embedding) VALUES (?, ?, ?, ?, ?)",
                                       (document, page, "Section " + str(page), "Private notes for " + workspace_id, vector))
                connection.execute("INSERT INTO memories(text, kind, created_at, embedding) VALUES (?, 'fact', ?, ?)",
                                   ("Memory for " + workspace_id, "2026-01-01", vector))
                db.put_file(connection, document, "notes.md", b"Original private note bytes")
            chat = chats.create_chat("Private chat")
            chats.add_message(chat, "user", "Question for " + workspace_id)
            message = chats.add_message(chat, "assistant", "Answer for " + workspace_id,
                                        sources=[{"doc_id": document, "doc_name": "notes-" + workspace_id + ".md", "page": 1, "text": "Private text"}])
            note = notes.create_note("Private note", "Private body", document)
            bookmark = notes.add_bookmark(document, "Section 1", "Private bookmark")
            plan = plans.create_plan("Private plan", {"markdown": "Review notes", "parameters": {"source_doc_ids": [document]}}, 2)
            fixtures[workspace_id] = {"folder": folder, "document": document, "chat": chat, "message": message,
                                      "note": note, "bookmark": bookmark, "plan": plan, "memory": memory.list_memories()[0]["id"]}

    def snapshot(workspace_id):
        with workspaces.workspace_context(workspace_id):
            return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id", fetch="all")]
                    for table in db.TABLES}

    def isolation():
        other = fixtures[ids[1]]
        before = snapshot(ids[1])
        own_before = snapshot(ids[0])
        with workspaces.workspace_context(ids[0]):
            for rows in (library.list_documents(), memory.list_memories(), chats.list_chats(), notes.list_notes(),
                         notes.list_bookmarks(), plans.list_plans(), db.execute("SELECT * FROM chunks", fetch="all")):
                require(all(row["workspace_id"] == ids[0] for row in rows if "workspace_id" in row))
            require(len(memory.list_memories()) == 1)
            require(memory.list_memories()[0]["text"] == "Memory for " + ids[0])
            require(all(row["id"] != other["document"] for row in library.list_documents()) if db.backend() == "postgres" else True)
            for table in db.TABLES:
                require(db.execute(f"SELECT * FROM {table} WHERE workspace_id = ?", (ids[1],), "all") == [])
            foreign_id_calls = (
                lambda: library.rename_document(other["document"], "Intruder"),
                lambda: library.rename_folder(other["folder"], "Intruder"),
                lambda: library.reorder_document(other["document"], "down"),
                lambda: library.move_document(fixtures[ids[0]]["document"], other["folder"]),
                lambda: chats.rename_chat(other["chat"], "Intruder"),
                lambda: chats.list_messages(other["chat"]),
                lambda: chats.set_chat_pinned(other["chat"]),
                lambda: chats.set_message_pinned(other["message"]),
                lambda: notes.update_note(other["note"], "Intruder", "Intruder"),
                lambda: notes.create_note("Intruder", "Body", other["document"]),
                lambda: notes.rename_bookmark(other["bookmark"], "Intruder"),
                lambda: plans.rename_plan(other["plan"], "Intruder"),
                lambda: plans.get_plan(other["plan"]),
                lambda: viewer.load_document(other["document"]),
                lambda: memory.update_memory_text(other["memory"], "Memory for " + ids[0]),
                lambda: notes.pin_note(other["note"]),
                lambda: plans.pin_plan(other["plan"]),
                lambda: notes.add_bookmark(other["document"], "Section 1"),
                lambda: chats.get_message(other["chat"], other["message"]),
                lambda: notes.delete_note(other["note"]),
                lambda: notes.delete_bookmark(other["bookmark"]),
                lambda: plans.delete_plan(other["plan"]),
                lambda: library.delete_folder(other["folder"]),
                lambda: chats.delete_chat(other["chat"]),
            )
            for action in foreign_id_calls:
                try:
                    result = action()
                except ValueError:
                    continue
                if isinstance(result, dict) and "workspace_id" in result:
                    require(result["workspace_id"] == ids[0])
                if isinstance(result, dict) and "document" in result:
                    require(result["document"]["workspace_id"] == ids[0])
                if isinstance(result, list):
                    require(all(row["workspace_id"] == ids[0] for row in result if "workspace_id" in row))
            for action in (
                lambda: db.get_file_bytes(ids[1], other["document"]),
                lambda: db.touch_workspace(ids[1]),
                lambda: db.clone_workspace(ids[1], uuid4().hex),
                lambda: db.reset_workspace(ids[1]),
                lambda: db.delete_workspace(ids[1]),
                lambda: db.execute("INSERT INTO notes(workspace_id, title, body, created_at, updated_at) VALUES (?, 'Intruder', '', '', '')", (ids[1],)),
            ):
                blocked(action)
            memory.delete_memory(other["memory"])
            memory.clear_memories()
            db.delete_document("notes-" + ids[1] + ".md")
            for table in db.TABLES:
                blocked(lambda table=table: db.execute(f"UPDATE {table} SET workspace_id = ? WHERE id = ?", (ids[1], 999999999)))
                db.execute(f"DELETE FROM {table} WHERE workspace_id = ?", (ids[1],))
        require(snapshot(ids[1]) == before)
        with workspaces.workspace_context(ids[0]), db._admin_session() as connection:
            db._clear_rows(connection, ids[0])
            for table in db.TABLES:
                for row in own_before[table]:
                    columns = list(row)
                    connection._run(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", tuple(row[column] for column in columns))
        db.invalidate_vectors(ids[0])

    def portable_sql():
        with workspaces.workspace_context(ids[0]):
            row = db.execute("SELECT '?' AS question, '100%' AS percent, ? AS value", ("50% ? value",), "one")
            require(row == {"question": "?", "percent": "100%", "value": "50% ? value"})

    def cloning():
        workspaces.ensure_template()
        with workspaces.workspace_context(ids[0]):
            db.clone_workspace("demo-template", ids[2])
            db.clone_workspace(ids[0], ids[3])
        with workspaces.workspace_context(ids[2]):
            require(bool(library.list_documents()))
            for table in db.TABLES:
                require(all(row["workspace_id"] == ids[2] for row in db.execute(f"SELECT * FROM {table}", fetch="all")))
        with workspaces.workspace_context(ids[3]):
            document = library.list_documents()[0]
            require(notes.list_notes()[0]["doc_id"] == document["id"])
            require(notes.list_bookmarks()[0]["doc_id"] == document["id"])
            require(plans.list_plans()[0]["body"]["parameters"]["source_doc_ids"] == [document["id"]])
            require(db.get_file_bytes(ids[3], document["id"]) == b"Original private note bytes")

    def cascade():
        with workspaces.workspace_context(ids[3]):
            document = library.list_documents()[0]
            db.delete_document(document["name"])
            require(notes.list_notes()[0]["doc_id"] is None)
            for table in ("chunks", "files", "bookmarks"):
                require(db.execute(f"SELECT COUNT(*) AS count FROM {table}", fetch="one")["count"] == 0)
            db.delete_workspace(ids[3])
        require(db._workspace_info(ids[3]) is None)

    def caps():
        with workspaces.workspace_context(ids[0]):
            maximum = config.MAX_WORKSPACE_BYTES
            try:
                with db.transaction() as connection:
                    config.MAX_WORKSPACE_BYTES = db.workspace_bytes(connection)
                    try:
                        db.put_file(connection, fixtures[ids[0]]["document"], "more.txt", b"x" * 1000)
                    except db.StorageLimitError:
                        pass
                    else:
                        raise AssertionError()
            finally:
                config.MAX_WORKSPACE_BYTES = maximum
            class Oversize:
                def __len__(self):
                    return config.MAX_FILE_BYTES + 1
            with db.transaction() as connection:
                try:
                    db.put_file(connection, fixtures[ids[0]]["document"], "large.txt", Oversize())
                except db.StorageLimitError:
                    pass
                else:
                    raise AssertionError()
            if db.backend() == "postgres":
                previous = config.DB_SOFT_LIMIT_MB
                try:
                    config.DB_SOFT_LIMIT_MB = 0
                    db._DATABASE_SIZE = (float("-inf"), 0)
                    try:
                        db.check_upload_guard()
                    except db.StorageLimitError:
                        pass
                    else:
                        raise AssertionError()
                finally:
                    config.DB_SOFT_LIMIT_MB = previous
                    db._DATABASE_SIZE = (float("-inf"), 0)

    def retention():
        db.create_workspace(ids[4])
        now = time.time()
        with workspaces.workspace_context(ids[4]):
            db.touch_workspace(ids[4], now - (config.WORKSPACE_RETENTION_DAYS + 1) * 86400)
        with db._workspace_connection() as connection:
            excluded = [row["id"] for row in connection._run("SELECT id FROM workspaces").fetchall() if row["id"] != ids[4]]
        workspaces.cleanup_workspaces(now=now, exclude=excluded)
        require(db._workspace_info(ids[4]) is None)

    def concurrent():
        errors = []
        def write(number):
            try:
                with workspaces.workspace_context(ids[0]):
                    chats.add_message(fixtures[ids[0]]["chat"], "assistant", f"Concurrent answer {number}")
            except Exception:
                errors.append(True)
        threads = [Thread(target=write, args=(number,), daemon=True) for number in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=40)
        require(not errors and not any(thread.is_alive() for thread in threads))
        with workspaces.workspace_context(ids[0]):
            rows = chats.list_messages(fixtures[ids[0]]["chat"])
            require(sum(row["content"].startswith("Concurrent answer ") for row in rows) == 3)

    try:
        if not check("migrations and fixtures", lambda: (seed(ids[0]), seed(ids[1]))):
            return 1
        check("workspace read/write/delete isolation", isolation)
        check("portable SQL placeholders and literal percent", portable_sql)
        if check("template and foreign-key cloning", cloning):
            check("document and workspace cascades", cascade)
        check("upload and storage caps", caps)
        check("retention cleanup", retention)
        check("three-thread concurrent writes", concurrent)
    finally:
        clean = True
        for workspace_id in ids:
            try:
                if db._workspace_info(workspace_id) is not None:
                    storage = workspaces.workspace_storage(workspace_id)
                    with workspaces.storage_context(storage.root, workspace_id):
                        db.delete_workspace(workspace_id)
            except Exception:
                clean = False
        print(("PASS" if clean else "FAIL") + " temporary workspace cleanup")
        if not clean:
            failed.append("cleanup")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("FAIL storage check")
        raise SystemExit(1) from None
