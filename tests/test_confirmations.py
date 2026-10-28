import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

with patch("dotenv.load_dotenv"):
    from src import chats, db, demo, library, memory, workflows, workspaces


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        storage = workspaces.storage_context(Path(temp.name))
        storage.__enter__()
        self.addCleanup(storage.__exit__, None, None, None)
        db.init_db()
        self.folder_id = library.create_folder("Course notes")
        with db.database_session(write=True) as connection:
            self.document_id = connection.execute(
                "INSERT INTO documents(name, uploaded_at, n_chunks, folder_id) VALUES ('notes.md', '2026-10-28', 1, ?)",
                (self.folder_id,),
            ).lastrowid
            connection.execute(
                "INSERT INTO chunks(doc_id, page, text, embedding) VALUES (?, 1, 'Original text.', ?)",
                (self.document_id, b"vector"),
            )
            self.memory_id = connection.execute(
                "INSERT INTO memories(text, kind, created_at) VALUES ('Exam on 5 Nov', 'deadline', '2026-10-28')"
            ).lastrowid
        self.chat_id = chats.create_chat("Current chat")
        chats.add_message(self.chat_id, "user", "Saved message")
        self.document = library.list_documents()[0]
        self.document_key = f"folder_{self.folder_id}_document_{self.document_id}"
        self.state = {"selected_chat_id": self.chat_id, "processed_files": {"notes.md": {"fingerprint": "fixture"}}}

    def counts(self):
        with db.database_session() as connection:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("documents", "folders", "chunks", "memories", "chats", "messages")}

    def actions(self):
        return [
            ("document", "notes.md", {"document": self.document, "document_key": self.document_key}),
            ("folder", "Course notes", self.folder_id),
            ("memory", "Exam on 5 Nov", self.memory_id),
            ("memories", "All memories", None),
            ("demo", "Reset demo", None),
            ("chat", "Current chat", self.chat_id),
        ]

    def request(self, kind, name, item=None):
        workflows.request_destructive_action(self.state, kind, name, item)
        return self.state["destructive_confirmation"]["token"]

    def test_every_first_click_and_cancel_preserve_data(self):
        before = self.counts()
        for kind, name, item in self.actions():
            with self.subTest(kind=kind):
                token = self.request(kind, name, item)
                self.assertEqual(self.counts(), before)
                self.assertFalse(workflows.confirm_destructive_action(self.state, token + 1))
                self.assertEqual(self.counts(), before)
                workflows.cancel_destructive_action(self.state, token)
                self.assertNotIn("destructive_confirmation", self.state)
                self.assertFalse(workflows.confirm_destructive_action(self.state, token))
                self.assertEqual(self.counts(), before)

    def test_document_deletes_only_after_confirmation(self):
        token = self.request(*self.actions()[0])
        self.assertEqual(self.counts()["documents"], 1)
        self.assertTrue(workflows.confirm_destructive_action(self.state, token))
        self.assertEqual(self.counts()["documents"], 0)
        self.assertEqual(self.counts()["chunks"], 0)
        self.assertNotIn("notes.md", self.state["processed_files"])
        self.assertEqual(self.state["uploader_version"], 1)
        self.assertFalse(workflows.confirm_destructive_action(self.state, token))

    def test_folder_confirmation_moves_documents_without_deleting(self):
        token = self.request("folder", "Course notes", self.folder_id)
        self.assertEqual(library.list_documents()[0]["folder_id"], self.folder_id)
        self.assertTrue(workflows.confirm_destructive_action(self.state, token))
        self.assertIsNone(library.list_documents()[0]["folder_id"])
        self.assertEqual(self.counts()["documents"], 1)
        self.assertEqual(self.counts()["chunks"], 1)
        self.assertEqual(self.counts()["folders"], 0)

    def test_single_memory_and_clear_all_require_confirmation(self):
        token = self.request("memory", "Exam on 5 Nov", self.memory_id)
        self.assertEqual(len(memory.list_memories()), 1)
        self.assertTrue(workflows.confirm_destructive_action(self.state, token))
        self.assertEqual(memory.list_memories(), [])
        with db.database_session(write=True) as connection:
            connection.executemany(
                "INSERT INTO memories(text, kind, created_at) VALUES (?, 'fact', '2026-10-28')",
                [("First fact",), ("Second fact",)],
            )
        token = self.request("memories", "All memories")
        self.assertEqual(len(memory.list_memories()), 2)
        self.assertTrue(workflows.confirm_destructive_action(self.state, token))
        self.assertEqual(memory.list_memories(), [])
        self.assertEqual(self.counts()["documents"], 1)
        self.assertEqual(self.counts()["chats"], 1)

    def test_reset_confirmation_clears_current_records_then_reseeds(self):
        token = self.request("demo", "Reset demo")
        before = self.counts()
        def restore_records():
            with db.database_session(write=True) as connection:
                for table in ("messages", "chats", "memories", "chunks", "documents", "folders"):
                    connection.execute(f"DELETE FROM {table}")
        with patch("src.workspaces.restore_workspace", side_effect=restore_records) as seed:
            self.assertEqual(self.counts(), before)
            seed.assert_not_called()
            self.assertTrue(workflows.confirm_destructive_action(self.state, token))
            seed.assert_called_once()
        self.assertTrue(all(count == 0 for count in self.counts().values()))
        self.assertNotIn("selected_chat_id", self.state)
        self.assertNotIn("destructive_confirmation", self.state)
        self.assertEqual(self.state["uploader_version"], 1)

    def test_active_chat_confirmation_keeps_fallback_behavior(self):
        other = chats.create_chat("Other chat")
        token = self.request("chat", "Current chat", self.chat_id)
        self.assertEqual(len(chats.list_chats()), 2)
        self.assertTrue(workflows.confirm_destructive_action(self.state, token))
        self.assertEqual(self.state["selected_chat_id"], other)
        self.assertEqual(len(chats.list_chats()), 1)
        token = self.request("chat", "Other chat", other)
        self.assertTrue(workflows.confirm_destructive_action(self.state, token))
        self.assertEqual(len(chats.list_chats()), 1)
        self.assertNotEqual(self.state["selected_chat_id"], other)
        self.assertEqual(chats.list_messages(self.state["selected_chat_id"]), [])

    def test_failed_delete_keeps_confirmation_available(self):
        token = self.request(*self.actions()[0])
        with patch.object(workflows, "delete_document", side_effect=RuntimeError("Unavailable")):
            self.assertFalse(workflows.confirm_destructive_action(self.state, token))
        self.assertEqual(self.state["destructive_confirmation"]["token"], token)
        self.assertIn("Unavailable", self.state["sidebar_error"])
        self.assertEqual(self.counts()["documents"], 1)
        workflows.cancel_destructive_action(self.state, token)
        self.assertNotIn("destructive_confirmation", self.state)

    def test_dialog_has_item_name_delete_cancel_and_required_explanations(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "show_delete_dialog")
        self.assertEqual(node.decorator_list[0].func.attr, "dialog")
        node.decorator_list = []

        class UI:
            def __init__(self, state):
                self.session_state, self.texts, self.buttons = state, [], []
            def text(self, text): self.texts.append(text)
            def write(self, text): self.texts.append(text)
            def error(self, text): self.texts.append(text)
            def columns(self, count): return [self] * count
            def button(self, label, **kwargs): self.buttons.append((label, kwargs))
        ui = UI(self.state)
        namespace = {"st": ui, "flow": workflows, "run_callback": lambda *args: None}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<delete-dialog>", "exec"), namespace)
        before = self.counts()
        for kind, name, item in self.actions():
            token = self.request(kind, name, item)
            ui.texts, ui.buttons = [], []
            namespace["show_delete_dialog"]()
            self.assertIn(name, ui.texts)
            self.assertEqual([label for label, _ in ui.buttons], ["Delete", "Cancel"])
            self.assertEqual(ui.buttons[0][1]["type"], "primary")
            self.assertIs(ui.buttons[0][1]["args"][0], workflows.confirm_destructive_action)
            self.assertIs(ui.buttons[1][1]["args"][0], workflows.cancel_destructive_action)
            if kind == "folder":
                self.assertIn("documents move to unfiled and are not deleted", " ".join(ui.texts))
            if kind == "demo":
                self.assertIn("replaces all current documents, memories and chats", " ".join(ui.texts))
            self.assertEqual(self.counts(), before)
            workflows.cancel_destructive_action(self.state, token)


if __name__ == "__main__":
    unittest.main()
