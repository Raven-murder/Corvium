import ast
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

with patch("dotenv.load_dotenv"):
    from src import chats, db, notes, viewer, workflows, workspaces


class NotesBookmarksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        context = workspaces.storage_context(self.root)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        db.init_db()
        self.doc_id = self.document("notes.md", [(1, "Atomicity"), (2, "Recovery")])

    def document(self, name, parts):
        with db.database_session(write=True) as connection:
            doc_id = connection.execute(
                "INSERT INTO documents(name, uploaded_at, n_chunks) VALUES (?, '2026-10-28', ?)", (name, len(parts))
            ).lastrowid
            connection.executemany("INSERT INTO chunks(doc_id, page, section, text, embedding) VALUES (?, ?, ?, ?, ?)",
                                   [(doc_id, page, section, "A transaction passage.", b"") for page, section in parts])
        return doc_id

    def test_note_crud_filter_pin_and_document_link(self):
        first = notes.create_note("Earlier", "Review atomicity.", self.doc_id)
        second = notes.create_note("Latest", "Review recovery.")
        self.assertEqual([n["id"] for n in notes.list_notes()], [second, first])
        original = notes.list_notes()[1]
        notes.pin_note(first)
        self.assertEqual([n["id"] for n in notes.list_notes()], [first, second])
        notes.update_note(first, "Exam revision", "Rollback keeps updates atomic.")
        note = notes.list_notes("ROLLBACK")[0]
        self.assertEqual((note["title"], note["doc_id"], note["doc_name"], note["pinned"]),
                         ("Exam revision", self.doc_id, "notes.md", 1))
        self.assertEqual(note["created_at"], original["created_at"])
        self.assertGreaterEqual(note["updated_at"], original["updated_at"])
        self.assertEqual(notes.list_notes("missing"), [])
        notes.pin_note(first, False)
        self.assertFalse(next(n for n in notes.list_notes() if n["id"] == first)["pinned"])
        notes.delete_note(first)
        self.assertEqual([n["id"] for n in notes.list_notes()], [second])
        with self.assertRaises(notes.NotesError):
            notes.update_note(first, "Gone", "Text")

    def test_bookmark_crud_optional_document_filter_and_duplicate_locator(self):
        first = notes.add_bookmark(self.doc_id, "Atomicity", "Review this")
        self.assertEqual(notes.add_bookmark(self.doc_id, {"page": 1, "section": "Atomicity"}), first)
        other_doc = self.document("slides.pptx", [(1, None), (2, "Recovery")])
        second = notes.add_bookmark(other_doc, "Slide 2")
        bookmarks = notes.list_bookmarks()
        self.assertEqual(len(bookmarks), 2)
        self.assertEqual(notes.list_bookmarks(self.doc_id)[0]["id"], first)
        slide = next(b for b in bookmarks if b["id"] == second)
        self.assertEqual((slide["locator"], slide["label"]), ("2", "Slide 2"))
        self.assertEqual(notes.current_bookmark(other_doc, {"slide": 2})["id"], second)
        notes.rename_bookmark(first, "Exam preparation")
        self.assertEqual(notes.list_bookmarks(self.doc_id)[0]["label"], "Exam preparation")
        state = {}
        workflows.open_bookmark(state, second)
        self.assertEqual(state["viewer_request"]["doc_id"], other_doc)
        self.assertEqual(state["viewer_request"]["locator"]["page"], 2)
        notes.delete_bookmark(first)
        self.assertEqual(notes.list_bookmarks(self.doc_id), [])
        with self.assertRaises(notes.NotesError):
            notes.rename_bookmark(first, "Gone")

    def test_document_delete_removes_bookmarks_keeps_detached_notes(self):
        note_id = notes.create_note("Keep this", "Independent revision text", self.doc_id)
        notes.pin_note(note_id)
        notes.add_bookmark(self.doc_id, "Recovery")
        db.delete_document("notes.md")
        self.assertEqual(notes.list_bookmarks(), [])
        note = notes.list_notes()[0]
        self.assertEqual((note["id"], note["doc_id"], note["body"], note["pinned"]),
                         (note_id, None, "Independent revision text", 1))
        self.assertIsNone(note["doc_name"])
        with db.database_session() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_foreign_key_actions_apply_to_direct_document_deletion(self):
        notes.create_note("Linked", "Keep body", self.doc_id)
        notes.add_bookmark(self.doc_id, "Atomicity")
        with db.database_session(write=True) as connection:
            connection.execute("DELETE FROM documents WHERE id = ?", (self.doc_id,))
        self.assertIsNone(notes.list_notes()[0]["doc_id"])
        self.assertEqual(notes.list_bookmarks(), [])

    def test_title_body_limits_and_invalid_records_do_not_write(self):
        valid = notes.create_note("T" * 120, "B" * 20_000)
        for operation in (lambda: notes.create_note("T" * 121, "body"),
                          lambda: notes.create_note("Title", "B" * 20_001),
                          lambda: notes.create_note(" ", "body"),
                          lambda: notes.create_note("Title", None),
                          lambda: notes.create_note("Title", "body", 999),
                          lambda: notes.update_note(valid, "Too long", "B" * 20_001),
                          lambda: notes.pin_note(valid, "false"),
                          lambda: notes.add_bookmark(999, "1"),
                          lambda: notes.add_bookmark(self.doc_id, ""),
                          lambda: notes.add_bookmark(self.doc_id, "Atomicity", "L" * 121)):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()
        self.assertEqual(len(notes.list_notes()), 1)
        self.assertEqual(len(notes.list_notes()[0]["body"]), 20_000)
        self.assertEqual(notes.list_bookmarks(), [])

    def test_note_cap_allows_updates_and_releases_a_slot_on_delete(self):
        with db.database_session(write=True) as connection:
            connection.executemany("INSERT INTO notes(title, body, created_at, updated_at) VALUES (?, '', 'date', 'date')",
                                   [(f"Note {i}",) for i in range(500)])
        with self.assertRaisesRegex(notes.NotesError, "notes are full"):
            notes.create_note("Overflow", "Text")
        note_id = notes.list_notes()[0]["id"]
        notes.update_note(note_id, "Updated at cap", "Body")
        notes.pin_note(note_id)
        notes.delete_note(note_id)
        notes.create_note("Replacement", "Body")
        self.assertEqual(len(notes.list_notes()), 500)

    def test_bookmark_cap_allows_rename_duplicates_and_releases_slot(self):
        doc_id = self.document("slides.pptx", [(1, None), (501, None)])
        with db.database_session(write=True) as connection:
            connection.executemany("INSERT INTO bookmarks(doc_id, locator, label, created_at) VALUES (?, ?, ?, 'date')",
                                   [(doc_id, str(i), f"Slide {i}") for i in range(1, 501)])
        with self.assertRaisesRegex(notes.NotesError, "bookmarks are full"):
            notes.add_bookmark(doc_id, "501")
        bookmark_id = notes.add_bookmark(doc_id, "2")
        notes.rename_bookmark(bookmark_id, "Renamed at cap")
        notes.delete_bookmark(bookmark_id)
        notes.add_bookmark(doc_id, "501")
        self.assertEqual(len(notes.list_bookmarks()), 500)

    def test_notes_and_bookmarks_are_isolated_per_workspace(self):
        notes.create_note("Private note", "First workspace", self.doc_id)
        notes.add_bookmark(self.doc_id, "Atomicity")
        state = {}
        viewer.request_viewer(state, self.doc_id)
        with workspaces.storage_context(self.root / "other"):
            self.assertEqual(notes.list_notes(), [])
            self.assertEqual(notes.list_bookmarks(), [])
            with self.assertRaises(ValueError):
                notes.create_note("Foreign document", "Body", self.doc_id)
            with self.assertRaises(ValueError):
                notes.add_bookmark(self.doc_id, "Atomicity")
            with self.assertRaises(ValueError):
                viewer.add_current_bookmark(state, "label")
            other = self.document("notes.md", [(1, "Other section")])
            notes.create_note("Other note", "Second workspace", other)
            notes.add_bookmark(other, "Other section")
            self.assertEqual(notes.list_notes()[0]["body"], "Second workspace")
        self.assertEqual(notes.list_notes()[0]["body"], "First workspace")
        self.assertEqual(notes.list_bookmarks()[0]["locator"], "Atomicity")

    def test_save_answer_attaches_only_a_cited_document_and_keeps_answer_text(self):
        chat = chats.create_chat()
        source = {"doc_id": self.doc_id, "doc_name": "notes.md", "page": 1, "text": "Atomic updates."}
        answer = "Transactions commit together. " + "Review this. " * 5 + "[notes.md, p.1]"
        message = chats.add_message(chat, "assistant", answer, sources=[source])
        state = {}
        workflows.save_answer_to_notes(state, chat, message)
        note = notes.list_notes()[0]
        self.assertEqual((note["title"], note["body"], note["doc_id"]), (answer[:50].strip(), answer, self.doc_id))
        self.assertEqual(state["note_saved_message"], (chat, message))
        uncited = chats.add_message(chat, "assistant", "An uncited answer.", sources=[source])
        notes.save_answer_note(chat, uncited)
        self.assertIsNone(notes.list_notes()[0]["doc_id"])
        question = chats.add_message(chat, "user", "My question")
        with self.assertRaises(notes.NotesError):
            notes.save_answer_note(chat, question)

    def test_viewer_bookmarks_numeric_pages_sections_and_optional_labels(self):
        doc_id = self.document("scan.pdf", [(1, None), (3, None)])
        state = {"bookmark_label": "Exam page"}
        viewer.request_viewer(state, doc_id, 2)
        viewer.add_current_bookmark(state, "bookmark_label")
        bookmark = notes.list_bookmarks(doc_id)[0]
        self.assertEqual((bookmark["locator"], bookmark["label"]), ("2", "Exam page"))
        self.assertEqual(state["bookmark_label"], "")
        self.assertEqual(notes.current_bookmark(doc_id, "Page 2")["id"], bookmark["id"])
        workflows.open_bookmark(state, bookmark["id"])
        self.assertEqual(state["viewer_request"]["locator"]["page"], 2)
        self.assertEqual(notes.locator_label("pptx", "2"), "Slide 2")
        self.assertEqual(notes.locator_label("text", "Atomicity"), "Atomicity")
        numeric_section = self.document("numbered.md", [(1, "100")])
        self.assertEqual(notes.list_bookmarks(numeric_section), [])
        notes.add_bookmark(numeric_section, "100")
        self.assertEqual(viewer.normalize_locator(viewer.load_document(numeric_section), "100")["section"], "100")

    def test_note_and_bookmark_deletion_require_confirmation(self):
        note_id = notes.create_note("Keep", "Body")
        bookmark_id = notes.add_bookmark(self.doc_id, "Recovery")
        state = {}
        for kind, item in (("note", note_id), ("bookmark", bookmark_id)):
            workflows.request_destructive_action(state, kind, "Item", item)
            token = state["destructive_confirmation"]["token"]
            self.assertEqual(len(notes.list_notes()), 1)
            self.assertEqual(len(notes.list_bookmarks()), 1)
            workflows.cancel_destructive_action(state, token)
        for kind, item in (("note", note_id), ("bookmark", bookmark_id)):
            workflows.request_destructive_action(state, kind, "Item", item)
            self.assertTrue(workflows.confirm_destructive_action(state, state["destructive_confirmation"]["token"]))
        self.assertEqual(notes.list_notes(), [])
        self.assertEqual(notes.list_bookmarks(), [])

    def test_note_ui_uses_safe_markdown_and_forms(self):
        notes.create_note("My note", "<script>unsafe()</script>**Study this**")
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "show_notes_panel")
        ui = MagicMock()
        ui.text_input.return_value = ""
        ui.session_state = {}
        namespace = {"st": ui, "notes": notes, "flow": workflows, "run_callback": Mock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<notes-ui>", "exec"), namespace)
        namespace["show_notes_panel"]()
        self.assertEqual(ui.markdown.call_args.args[0], "<script>unsafe()</script>**Study this**")
        self.assertFalse(ui.markdown.call_args.kwargs.get("unsafe_allow_html", False))
        delete = next(call for call in ui.button.call_args_list if call.kwargs.get("help") == "Delete note")
        self.assertIs(delete.kwargs["args"][0], workflows.request_destructive_action)


class NotesMigrationTests(unittest.TestCase):
    def test_old_database_and_repeated_migration_preserve_existing_data(self):
        with tempfile.TemporaryDirectory() as temporary, workspaces.storage_context(temporary):
            connection = sqlite3.connect(Path(temporary) / "app.db")
            connection.executescript("""
                CREATE TABLE documents (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, uploaded_at TEXT NOT NULL, n_chunks INTEGER NOT NULL);
                INSERT INTO documents VALUES (12, 'old.md', '2026-01-01', 0);
            """)
            connection.close()
            db.init_db()
            note_id = notes.create_note("Legacy document note", "Keep existing text", 12)
            bookmark_id = notes.add_bookmark(12, "1")
            notes.pin_note(note_id)
            before = notes.list_notes(), notes.list_bookmarks()
            db.init_db()
            db.init_db()
            self.assertEqual((notes.list_notes(), notes.list_bookmarks()), before)
            with db.database_session() as connection:
                self.assertEqual(connection.execute("SELECT name FROM documents WHERE id = 12").fetchone()[0], "old.md")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(bookmark_id, notes.list_bookmarks()[0]["id"])


if __name__ == "__main__":
    unittest.main()
