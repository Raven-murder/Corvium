import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

with patch("dotenv.load_dotenv"):
    from src import actions, chats, db, demo, ingest, library, qa, workspaces


LEGACY_SCHEMA = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    uploaded_at TEXT NOT NULL, n_chunks INTEGER NOT NULL
);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page INTEGER NOT NULL, text TEXT NOT NULL, embedding BLOB NOT NULL
);
CREATE TABLE memories (
    id INTEGER PRIMARY KEY, text TEXT NOT NULL, kind TEXT,
    created_at TEXT NOT NULL, embedding BLOB
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        storage = workspaces.storage_context(self.folder)
        storage.__enter__()
        self.addCleanup(storage.__exit__, None, None, None)
        db.init_db()

    def add_document(self, name):
        with db.database_session(write=True) as connection:
            document_id = connection.execute(
                "INSERT INTO documents(name, uploaded_at, n_chunks) VALUES (?, '2026-10-27', 1)", (name,)
            ).lastrowid
            connection.execute(
                "INSERT INTO chunks(doc_id, page, text, embedding) VALUES (?, 1, 'Sample note', ?)",
                (document_id, db.embedding_to_blob(np.ones(4096, dtype=np.float32))),
            )
        return document_id

    def test_folder_create_rename_delete_preserves_documents(self):
        first = library.create_folder("Database notes")
        second = library.create_folder("Revision")
        library.rename_folder(first, "Databases")
        self.assertEqual([f["name"] for f in library.list_library()["folders"]], ["Databases", "Revision"])
        unfiled = self.add_document("unfiled.md")
        moved = self.add_document("inside.md")
        library.move_document(moved, first)
        library.delete_folder(first)
        result = library.list_library()
        self.assertEqual([d["id"] for d in result["unfiled"]], [unfiled, moved])
        self.assertEqual([f["id"] for f in result["folders"]], [second])
        with db.database_session() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_document_rename_move_and_reorder(self):
        folder = library.create_folder("Topics")
        ids = [self.add_document(name) for name in ("a.md", "b.pdf", "c.md")]
        library.rename_document(ids[1], "Transactions")
        for document_id in ids:
            library.move_document(document_id, folder)
        library.reorder_document(ids[1], "up")
        self.assertEqual([d["id"] for d in library.list_library()["folders"][0]["documents"]], [ids[1], ids[0], ids[2]])
        library.reorder_document(ids[1], "up")
        library.reorder_document(ids[1], "down")
        library.reorder_document(ids[2], "down")
        library.move_document(ids[1], None)
        library.move_document(ids[0], folder)
        result = library.list_library()
        self.assertEqual([d["id"] for d in result["folders"][0]["documents"]], [ids[0], ids[2]])
        renamed = result["unfiled"][0]
        self.assertEqual(renamed["display_name"], "Transactions")
        self.assertEqual(renamed["name"], "b.pdf")
        with db.database_session() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 3)
        with self.assertRaises(ValueError):
            library.move_document(ids[0], 999)
        with self.assertRaises(ValueError):
            library.reorder_document(ids[0], "sideways")
        with self.assertRaises(ValueError):
            library.rename_document(ids[0], " ")

    def test_images_have_separate_group(self):
        folder = library.create_folder("Notes")
        note = self.add_document("text.md")
        first = self.add_document("diagram.PNG")
        second = self.add_document("photo.jpeg")
        library.move_document(first, folder)
        result = library.list_library()
        self.assertEqual({d["id"] for d in result["images"]}, {first, second})
        self.assertEqual([d["id"] for d in result["unfiled"]], [note])
        self.assertEqual(result["folders"][0]["documents"], [])
        for doc in result["images"]:
            self.assertEqual(doc["kind"], "image")

    def test_ingestion_preserves_metadata_and_uses_display_citations(self):
        path = self.folder / "notes.md"
        path.write_text("First version", encoding="utf-8")
        vector = np.ones((1, 4096), dtype=np.float32)
        with patch.object(ingest, "embed_texts", return_value=vector):
            ingest.ingest_file(path)
            document = library.list_documents()[0]
            folder = library.create_folder("Course")
            library.rename_document(document["id"], "My database notes")
            library.move_document(document["id"], folder)
            path.write_text("Updated material", encoding="utf-8")
            ingest.ingest_file(path)
            updated = library.list_documents()[0]
            self.assertEqual(updated["id"], document["id"])
            self.assertEqual(updated["folder_id"], folder)
            self.assertEqual(updated["display_name"], "My database notes")
            stored = self.folder / "files" / f"{document['id']}.md"
            self.assertEqual((self.folder / updated["stored_path"]).resolve(), stored.resolve())
            self.assertEqual(stored.read_bytes(), path.read_bytes())
            self.assertEqual(updated["kind"], "text")
            self.assertEqual(ingest.search("database")[0]["doc_name"], "My database notes")
            self.assertEqual(actions._sample_chunks()[0]["doc_name"], "My database notes")
        db.delete_document("notes.md")
        with db.database_session() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)

    def test_chat_crud_titles_and_messages(self):
        first, second = chats.create_chat(), chats.create_chat("Other")
        self.assertEqual(chats.list_messages(first), [])
        sources = [{"doc_name": "Note", "page": 1, "text": "Evidence", "score": 1.0}]
        chats.add_message(first, "assistant", "Welcome")
        self.assertEqual(next(c for c in chats.list_chats() if c["id"] == first)["title"], "New chat")
        question = "Explain normalization and database transactions in detail"
        user_id = chats.add_message(first, "user", question)
        chats.add_message(first, "assistant", "Answer", sources=sources)
        chats.add_message(first, "assistant", "Failure", error=True)
        self.assertEqual(chats.list_chats()[0]["title"], question[:40])
        messages = chats.list_messages(first)
        self.assertEqual(messages[1]["id"], user_id)
        self.assertEqual(messages[2]["sources"], sources)
        self.assertTrue(messages[3]["error"])
        self.assertEqual(chats.list_messages(second), [])
        chats.rename_chat(first, "Revision")
        chats.add_message(first, "user", "Another question")
        self.assertEqual(chats.list_chats()[0]["title"], "Revision")
        chats.add_message(second, "user", "Other question")
        chats.delete_chat(first)
        self.assertEqual([c["id"] for c in chats.list_chats()], [second])
        self.assertEqual(len(chats.list_messages(second)), 1)
        with self.assertRaises(ValueError):
            chats.list_messages(first)
        with self.assertRaises(ValueError):
            chats.add_message(first, "user", "Deleted chat")
        with self.assertRaises(ValueError):
            chats.rename_chat(second, " ")
        with db.database_session() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (first,)).fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_qa_uses_only_selected_chat_history(self):
        first, second = chats.create_chat(), chats.create_chat()
        chats.add_message(first, "user", "First chat context")
        chats.add_message(second, "user", "Other chat context")
        result = {"answer": "An answer", "sources": []}
        with patch.object(qa, "_answer_question", return_value=result) as answer:
            self.assertEqual(qa.answer_question("Follow up", chat_id=first), result)
            history = answer.call_args.args[2]
            self.assertEqual([m["content"] for m in history], ["First chat context"])
        self.assertEqual([m["content"] for m in chats.list_messages(first)], ["First chat context", "Follow up", "An answer"])
        self.assertEqual([m["content"] for m in chats.list_messages(second)], ["Other chat context"])
        with patch.object(qa, "_answer_question", side_effect=RuntimeError("Unavailable")):
            with self.assertRaises(RuntimeError):
                qa.answer_question("Retry", chat_id=second)
        self.assertTrue(chats.list_messages(second)[-1]["error"])

    def test_qa_sends_selected_history_to_model(self):
        first, second = chats.create_chat(), chats.create_chat()
        chats.add_message(first, "user", "Selected context")
        chats.add_message(second, "user", "Unselected context")
        source = {"doc_name": "Note", "page": 1, "text": "Evidence", "score": 1.0}
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Answer [Note, p.1]"))])
        with patch.object(qa, "search", return_value=[source]), patch.object(qa, "_remember_question", return_value=("Nothing saved", False)), patch.object(qa, "get_relevant_memories", return_value=[]), patch.object(qa, "get_client") as client:
            client.return_value.chat.completions.create.return_value = response
            qa.answer_question("Explain", chat_id=first)
            sent = client.return_value.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(sent[1]["content"], "Selected context")
        self.assertNotIn("Unselected context", str(sent))

    def test_reset_delegates_to_current_workspace_template(self):
        with patch("src.workspaces.restore_workspace") as restore:
            demo.reset_demo()
            restore.assert_called_once()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "app.db"
        storage = workspaces.storage_context(self.path.parent)
        storage.__enter__()
        self.addCleanup(storage.__exit__, None, None, None)
        connection = sqlite3.connect(self.path)
        connection.executescript(LEGACY_SCHEMA)
        names = ["note.pdf", "letter.docx", "slides.pptx", "notes.md", "page.HTML", "diagram.PNG"]
        connection.executemany(
            "INSERT INTO documents(id, name, uploaded_at, n_chunks) VALUES (?, ?, '2026-10-20', 1)",
            enumerate(names, 1),
        )
        connection.execute("INSERT INTO chunks VALUES (17, 1, 3, 'Preserved content', ?)", (b"original-vector",))
        connection.execute("INSERT INTO memories VALUES (11, 'Weak at normalization', 'weak_topic', '2026-10-20', ?)", (b"memory-vector",))
        connection.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", [
            (7, "session-a", "user", "First question", "2026-10-20"),
            (9, "session-b", "assistant", "Earlier answer", "2026-10-21"),
        ])
        connection.commit()
        connection.close()

    def test_legacy_migration_is_idempotent_and_preserves_data(self):
        db.init_db()
        db.init_db()
        documents = library.list_documents()
        self.assertEqual({d["name"]: d["kind"] for d in documents}, {
            "note.pdf": "pdf", "letter.docx": "docx", "slides.pptx": "pptx",
            "notes.md": "text", "page.HTML": "html", "diagram.PNG": "image",
        })
        self.assertTrue(all(d["display_name"] == d["name"] for d in documents))
        self.assertTrue(all(d["stored_path"] is None and d["folder_id"] is None for d in documents))
        self.assertEqual(len({d["position"] for d in documents}), 6)
        migrated = chats.list_chats()
        self.assertEqual(len(migrated), 1)
        self.assertEqual(migrated[0]["title"], "Earlier chat")
        messages = chats.list_messages(migrated[0]["id"])
        self.assertEqual([m["id"] for m in messages], [7, 9])
        self.assertEqual([m["session_id"] for m in messages], ["session-a", "session-b"])
        self.assertEqual([m["content"] for m in messages], ["First question", "Earlier answer"])
        with db.database_session() as connection:
            chunk = connection.execute("SELECT * FROM chunks").fetchone()
            self.assertEqual((chunk["id"], chunk["text"], chunk["embedding"], chunk["section"]), (17, "Preserved content", b"original-vector", None))
            self.assertEqual(connection.execute("SELECT embedding FROM memories WHERE id = 11").fetchone()[0], b"memory-vector")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        library.rename_document(1, "Renamed note")
        library.move_document(1, library.create_folder("New folder"))
        db.init_db()
        self.assertEqual(next(d for d in library.list_documents() if d["id"] == 1)["display_name"], "Renamed note")
        self.assertEqual(len(chats.list_chats()), 1)

    def test_failed_migration_rolls_back_schema_and_records(self):
        with patch.object(db, "infer_document_kind", side_effect=RuntimeError("Migration interrupted")):
            with self.assertRaises(RuntimeError):
                db.init_db()
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
        self.assertNotIn("folder_id", columns)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 6)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
        db.init_db()
        self.assertEqual(len(chats.list_chats()), 1)


if __name__ == "__main__":
    unittest.main()
