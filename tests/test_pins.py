import ast
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

with patch("dotenv.load_dotenv"):
    from src import chats, db, workflows, workspaces


QUIZ = [{"question": f"Which option is correct for question {i}?", "options": ["A", "B", "C", "D"],
         "answer_index": 2, "explanation": "C is correct.", "source_doc": "Notes", "source_page": 1} for i in range(3)]


class PinTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        storage = workspaces.storage_context(self.root)
        storage.__enter__()
        self.addCleanup(storage.__exit__, None, None, None)
        db.init_db()
        self.first = chats.create_chat("First chat")
        self.second = chats.create_chat("Second chat")
        self.third = chats.create_chat("Third chat")

    def test_chat_pins_sort_by_pin_order_and_unpin_preserves_activity_order(self):
        with patch.object(chats, "_now", return_value="2027-01-01T00:00:01+00:00"):
            chats.set_chat_pinned(self.second)
        with patch.object(chats, "_now", return_value="2027-01-01T00:00:02+00:00"):
            chats.set_chat_pinned(self.first)
        self.assertEqual([c["id"] for c in chats.list_chats()], [self.second, self.first, self.third])
        with patch.object(chats, "_now", return_value="2028-01-01T00:00:00+00:00"):
            chats.add_message(self.first, "user", "New activity")
        before = chats.list_chats()
        with patch.object(chats, "_now") as clock:
            chats.set_chat_pinned(self.second, True)
            clock.assert_not_called()
        self.assertEqual(chats.list_chats(), before)
        self.assertTrue(chats.chat_label(before[0]).startswith("📌 "))
        self.assertEqual(chats.chat_label(before[-1]), "Third chat")
        chats.set_chat_pinned(self.second, False)
        rows = chats.list_chats()
        self.assertEqual([c["id"] for c in rows], [self.first, self.third, self.second])
        self.assertIsNone(next(c for c in rows if c["id"] == self.second)["pinned_at"])
        with patch.object(chats, "_now", return_value="2029-01-01T00:00:00+00:00"):
            chats.set_chat_pinned(self.second)
        self.assertEqual([c["id"] for c in chats.list_chats()], [self.first, self.second, self.third])

    def test_message_pin_listing_spans_chats_preserves_full_payload_and_current_title(self):
        sources = [{"doc_name": "Notes", "page": 1, "text": "A source sentence."}]
        first_message = chats.add_message(self.first, "user", "A" * 100)
        second_message = chats.add_message(self.second, "assistant", "Saved answer", sources=sources, kind="plan")
        self.assertEqual(chats.list_pinned_messages(), [])
        with patch.object(chats, "_now", return_value="2027-01-01T00:00:01+00:00"):
            chats.set_message_pinned(second_message)
        with patch.object(chats, "_now", return_value="2027-01-01T00:00:02+00:00"):
            chats.set_message_pinned(first_message)
        pinned = chats.list_pinned_messages()
        self.assertEqual([m["id"] for m in pinned], [second_message, first_message])
        self.assertEqual(pinned[0]["chat_title"], "Second chat")
        self.assertEqual(pinned[0]["sources"], sources)
        self.assertEqual(pinned[0]["kind"], "plan")
        self.assertEqual(len(pinned[1]["content"]), 100)
        self.assertEqual(chats.pinned_message_label(pinned[1]), "A" * 80 + "…")
        stamp = pinned[1]["pinned_at"]
        chats.set_message_pinned(first_message, True)
        self.assertEqual(chats.list_pinned_messages()[1]["pinned_at"], stamp)
        chats.rename_chat(self.second, "Renamed chat")
        self.assertEqual(chats.list_pinned_messages()[0]["chat_title"], "Renamed chat")
        chats.set_message_pinned(second_message, False)
        self.assertEqual([m["id"] for m in chats.list_pinned_messages()], [first_message])
        self.assertIsNone(chats.get_message(self.second, second_message)["pinned_at"])
        chats.delete_chat(self.first)
        self.assertEqual(chats.list_pinned_messages(), [])

    def test_pin_validation_and_workspace_isolation(self):
        message = chats.add_message(self.first, "assistant", "Private message")
        chats.set_chat_pinned(self.first)
        chats.set_message_pinned(message)
        for action in (lambda: chats.set_chat_pinned(999), lambda: chats.set_message_pinned(999),
                       lambda: chats.set_chat_pinned(self.first, "false"), lambda: chats.set_message_pinned(message, 2)):
            with self.assertRaises(ValueError):
                action()
        with workspaces.storage_context(self.root / "other"):
            other = chats.create_chat("Other workspace")
            self.assertEqual(chats.list_pinned_messages(), [])
            self.assertFalse(chats.list_chats()[0]["pinned"])
            other_message = chats.add_message(other, "assistant", "Other workspace message")
            chats.set_message_pinned(other_message)
            self.assertEqual(chats.list_pinned_messages()[0]["content"], "Other workspace message")
        self.assertEqual(chats.list_pinned_messages()[0]["content"], "Private message")

    def test_fresh_chat_and_delete_fallback_use_activity_not_pin_order(self):
        chats.add_message(self.first, "user", "Pinned old chat")
        chats.set_chat_pinned(self.first)
        empty = chats.create_chat("Most recent empty")
        self.assertEqual(chats.initial_chat_id(chats.list_chats()), empty)
        newest = chats.create_chat("Most recent remaining")
        chats.add_message(newest, "user", "Latest activity")
        state = {"selected_chat_id": empty}
        workflows.delete_chat_from_sidebar(state, empty, confirmed=True)
        self.assertEqual(state["selected_chat_id"], newest)
        self.assertEqual(chats.list_chats()[0]["id"], self.first)

    def test_pinned_dialog_navigation_and_close_keep_full_message(self):
        message_id = chats.add_message(self.second, "assistant", "A full pinned answer " * 10)
        chats.set_message_pinned(message_id)
        state = {"selected_chat_id": self.first, "view": "plan", "sources_panel": {"old": True}}
        workflows.open_pinned_message(state, self.second, message_id)
        self.assertNotIn("sources_panel", state)
        self.assertEqual(workflows.active_pinned_message(state)["content"], "A full pinned answer " * 10)
        workflows.close_pinned_message(state)
        self.assertEqual(state["selected_chat_id"], self.first)
        workflows.open_pinned_message(state, self.second, message_id)
        workflows.go_to_pinned_chat(state)
        self.assertEqual(state["selected_chat_id"], self.second)
        self.assertEqual(state["view"], "chat")
        self.assertNotIn("pinned_message_view", state)
        workflows.open_pinned_message(state, self.second, message_id)
        chats.delete_chat(self.second)
        self.assertIsNone(workflows.active_pinned_message(state))
        self.assertIn("no longer available", state["chat_notice"])

    def test_pinned_quiz_from_another_chat_grades_and_synchronizes_choices(self):
        message_id = chats.add_message(self.second, "assistant", json.dumps(QUIZ), kind="quiz")
        message = chats.get_message(self.second, message_id)
        chats.set_message_pinned(message_id)
        state = {"selected_chat_id": self.first}
        workflows.open_pinned_message(state, self.second, message_id)
        for index in range(3):
            normal = workflows.quiz_choice_key(message, index + 1)
            pinned = workflows.quiz_choice_key(message, index + 1, "pinned")
            state[normal], state[pinned] = 0, 2
            workflows.choose_quiz_answer(state, message_id, index, pinned, self.second)
            self.assertEqual(state[normal], 2)
        workflows.check_quiz_answers(state, message_id, self.second)
        self.assertEqual(state["quiz_answers"][message_id], [2, 2, 2])
        self.assertEqual(state["selected_chat_id"], self.first)

    def test_message_and_pinned_quiz_widgets_have_unique_keys_and_pin_styles(self):
        message_id = chats.add_message(self.second, "assistant", json.dumps(QUIZ), kind="quiz")
        chats.set_message_pinned(message_id)
        message = chats.get_message(self.second, message_id)
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        names = {"show_chat_message", "show_quiz_result", "show_message_pin"}
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        ui = MagicMock()
        ui.session_state = {"selected_chat_id": self.second}
        ui.button.return_value = False
        namespace = {"st": ui, "flow": workflows, "run_callback": Mock(), "show_sources": Mock(),
                     "show_answer": Mock(), "safe_error": str}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<pinned-quiz>", "exec"), namespace)
        namespace["show_chat_message"](message, self.second)
        namespace["show_chat_message"](message, self.second, scope="pinned")
        radio_keys = [call.kwargs["key"] for call in ui.radio.call_args_list]
        button_keys = [call.kwargs["key"] for call in ui.button.call_args_list]
        self.assertEqual(len(radio_keys), len(set(radio_keys)))
        self.assertEqual(len(button_keys), len(set(button_keys)))
        pin_calls = [call for call in ui.button.call_args_list if call.kwargs.get("icon") == ":material/push_pin:"]
        self.assertEqual(len(pin_calls), 2)
        self.assertTrue(all(call.kwargs["key"].startswith("pin_on_") for call in pin_calls))
        self.assertTrue(all(call.kwargs["args"][0] is workflows.pin_message_from_view for call in pin_calls))
        for key in radio_keys:
            ui.session_state[key] = 2
        ui.radio.reset_mock()
        namespace["show_chat_message"](message, self.second)
        namespace["show_chat_message"](message, self.second, scope="pinned")
        self.assertTrue(all(call.kwargs["index"] is None for call in ui.radio.call_args_list))

    def test_pinned_sidebar_preview_opens_the_correct_message(self):
        message_id = chats.add_message(self.second, "user", "Preview " * 20)
        chats.set_message_pinned(message_id)
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "show_pinned_panel")
        ui = MagicMock()
        namespace = {"st": ui, "flow": workflows, "run_callback": Mock(),
                     "list_pinned_messages": chats.list_pinned_messages, "pinned_message_label": chats.pinned_message_label}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<pins-sidebar>", "exec"), namespace)
        namespace["show_pinned_panel"]()
        ui.caption.assert_called_once_with(next(chat["title"] for chat in chats.list_chats() if chat["id"] == self.second))
        self.assertEqual(ui.button.call_args.kwargs["args"], (workflows.open_pinned_message, self.second, message_id))
        self.assertLessEqual(len(ui.button.call_args.args[0]), 81)


class PinMigrationTests(unittest.TestCase):
    def test_old_database_gets_defaults_and_repeated_migration_preserves_pins_and_data(self):
        with tempfile.TemporaryDirectory() as temporary, workspaces.storage_context(temporary):
            connection = sqlite3.connect(Path(temporary) / "app.db")
            connection.executescript("""
                CREATE TABLE chats (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, chat_id INTEGER REFERENCES chats(id),
                    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'text',
                    sources TEXT NOT NULL DEFAULT '[]', error INTEGER NOT NULL DEFAULT 0);
                INSERT INTO chats VALUES (11, 'Existing chat', '2026-10-20', '2026-10-21');
                INSERT INTO messages VALUES (17, 'legacy', 11, 'assistant', 'Preserved plan', '2026-10-21', 'plan', '[]', 0);
            """)
            connection.close()
            db.init_db()
            db.init_db()
            chat = chats.list_chats()[0]
            message = chats.list_messages(11)[0]
            self.assertEqual((chat["id"], chat["title"], chat["pinned"], chat["pinned_at"]), (11, "Existing chat", 0, None))
            self.assertEqual((message["id"], message["content"], message["kind"], message["pinned"], message["pinned_at"]),
                             (17, "Preserved plan", "plan", 0, None))
            chats.set_chat_pinned(11)
            chats.set_message_pinned(17)
            before = chats.list_chats(), chats.list_pinned_messages()
            db.init_db()
            db.init_db()
            self.assertEqual((chats.list_chats(), chats.list_pinned_messages()), before)
            with db.database_session() as connection:
                for table in ("chats", "messages"):
                    columns = {row["name"]: row for row in connection.execute(f"PRAGMA table_info({table})")}
                    self.assertEqual(columns["pinned"]["dflt_value"], "0")
                    self.assertIn("pinned_at", columns)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
