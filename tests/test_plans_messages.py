import ast
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

with patch("dotenv.load_dotenv"):
    from src import actions, chats, db, plans, workflows, workspaces


QUIZ = [
    {"question": f"What happens after failed update {i}?", "options": ["Commit", "Ignore", "Rollback", "Retry forever"],
     "answer_index": 2, "explanation": "A failed transaction rolls back.", "source_doc": "Notes", "source_page": 1}
    for i in range(3)
]


class PlansMessagesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        storage = workspaces.storage_context(self.root)
        storage.__enter__()
        self.addCleanup(storage.__exit__, None, None, None)
        db.init_db()
        self.chat_id = chats.create_chat()
        self.state = {"selected_chat_id": self.chat_id}

    def create_plan(self, title="Revision", days=2):
        return plans.create_plan(title, {"markdown": "Review transactions.", "parameters": {"days": days}}, days)

    def test_plans_crud_and_single_pin_preserve_prior_versions(self):
        first = self.create_plan("Earlier")
        second = self.create_plan("Latest")
        self.assertEqual([plan["id"] for plan in plans.list_plans()], [second, first])
        plans.rename_plan(first, "Exam revision")
        plans.pin_plan(first)
        self.assertEqual(plans.list_plans()[0]["id"], first)
        before = plans.get_plan(first)
        db.init_db()
        db.init_db()
        self.assertEqual(plans.get_plan(first), before)
        self.assertEqual(before["title"], "Exam revision")
        self.assertEqual(json.loads(before["body_json"])["markdown"], "Review transactions.")
        plans.pin_plan(second)
        self.assertEqual([p["id"] for p in plans.list_plans() if p["pinned"]], [second])
        with self.assertRaises(ValueError):
            plans.pin_plan(999)
        self.assertTrue(plans.get_plan(second)["pinned"])
        plans.pin_plan(second, False)
        self.assertFalse(any(p["pinned"] for p in plans.list_plans()))
        plans.delete_plan(second)
        self.assertEqual([p["id"] for p in plans.list_plans()], [first])
        for operation in (lambda: plans.get_plan(second), lambda: plans.rename_plan(first, " "),
                          lambda: plans.create_plan("Bad", {}, 2), lambda: self.create_plan(days=0)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(plans.get_plan(first)["body"], before["body"])

    def test_plan_storage_is_isolated_between_workspaces(self):
        plan_id = self.create_plan("Private plan")
        with workspaces.storage_context(self.root / "other"):
            self.assertEqual(plans.list_plans(), [])
            with self.assertRaises(ValueError):
                plans.get_plan(plan_id)
            other = self.create_plan("Other plan")
            plans.pin_plan(other)
        self.assertEqual([p["title"] for p in plans.list_plans()], ["Private plan"])
        self.assertFalse(plans.get_plan(plan_id)["pinned"])

    def test_plan_and_quiz_append_after_text_in_selected_chat(self):
        chats.add_message(self.chat_id, "user", "Explain transactions")
        chats.add_message(self.chat_id, "assistant", "Transactions group updates.")
        other = chats.create_chat("Other chat")
        with patch.object(workflows, "generate_study_plan", return_value="Review commits."):
            saved = workflows.run_tool(self.state, "plan", {"days": 2, "focus_topics": ["Atomicity"], "source_doc_ids": [1]})
        with patch.object(workflows, "generate_quiz", return_value=QUIZ):
            workflows.run_tool(self.state, "quiz", {"n": 3})
        messages = chats.list_messages(self.chat_id)
        self.assertEqual([m["kind"] for m in messages], ["text", "text", "plan", "quiz"])
        self.assertEqual([m["id"] for m in messages], sorted(m["id"] for m in messages))
        self.assertEqual(messages[-2]["id"], saved["message_id"])
        self.assertEqual(json.loads(messages[-1]["content"]), QUIZ)
        self.assertEqual(chats.list_messages(other), [])
        self.assertEqual(plans.get_plan(saved["plan_id"])["body"]["parameters"]["source_doc_ids"], [1])
        self.assertEqual(self.state["view"], "chat")
        with self.assertRaises(ValueError):
            chats.add_message(self.chat_id, "assistant", "Invalid", kind="unknown")

    def test_quiz_answers_survive_reruns_and_are_kept_per_message(self):
        message_id = chats.add_message(self.chat_id, "assistant", json.dumps(QUIZ), kind="quiz")
        message = chats.get_message(self.chat_id, message_id)
        self.assertEqual(workflows.quiz_selections(self.state, message), [None, None, None])
        with self.assertRaises(ValueError):
            workflows.check_quiz_answers(self.state, message_id)
        for index in range(3):
            self.state["choice"] = 2
            workflows.choose_quiz_answer(self.state, message_id, index, "choice")
        workflows.check_quiz_answers(self.state, message_id)
        self.assertEqual(self.state["quiz_answers"][message_id], [2, 2, 2])
        self.assertEqual(actions.grade_quiz(QUIZ, self.state["quiz_answers"][message_id]), (3, [True, True, True]))
        self.state.pop("choice")
        self.assertEqual(workflows.quiz_selections(self.state, chats.get_message(self.chat_id, message_id)), [2, 2, 2])
        with patch.object(workflows, "generate_quiz", return_value=QUIZ):
            result = workflows.run_tool(self.state, "quiz", {})
        self.assertIn(message_id, self.state["quiz_answers"])
        next_message = chats.get_message(self.chat_id, result["message_id"])
        self.assertEqual(workflows.quiz_selections(self.state, next_message), [None] * 3)
        self.state["choice"] = 0
        workflows.choose_quiz_answer(self.state, message_id, 0, "choice")
        self.assertNotIn(message_id, self.state["quiz_answers"])
        other = chats.create_chat()
        self.state["selected_chat_id"] = other
        with self.assertRaises(ValueError):
            workflows.check_quiz_answers(self.state, message_id)

    def test_reused_sqlite_message_ids_do_not_reuse_another_chats_quiz_answers(self):
        message_id = chats.add_message(self.chat_id, "assistant", json.dumps(QUIZ), kind="quiz")
        message = chats.get_message(self.chat_id, message_id)
        workflows.quiz_selections(self.state, message)
        for index in range(3):
            self.state["choice"] = 2
            workflows.choose_quiz_answer(self.state, message_id, index, "choice")
        workflows.check_quiz_answers(self.state, message_id)
        chats.delete_chat(self.chat_id)
        other = chats.create_chat()
        reused = chats.add_message(other, "assistant", json.dumps(QUIZ), kind="quiz")
        self.assertEqual(reused, message_id)
        self.state["selected_chat_id"] = other
        self.assertEqual(workflows.quiz_selections(self.state, chats.get_message(other, reused)), [None] * 3)
        self.assertNotIn(reused, self.state["quiz_answers"])

    def test_regeneration_retains_settings_saves_version_and_moves_pin(self):
        parameters = {"days": 3, "focus_topics": ["Transactions"], "source_doc_ids": [1, 2]}
        with patch.object(workflows, "generate_study_plan", return_value="First plan"):
            first = workflows.run_tool(self.state, "plan", parameters)["plan_id"]
        plans.rename_plan(first, "Exam preparation")
        plans.pin_plan(first)
        workflows.open_plan_view(self.state)
        self.assertEqual(self.state["selected_plan_id"], first)
        workflows.queue_plan_regeneration(self.state, first)
        self.assertEqual(self.state.pop("regenerate_plan_id"), first)
        with patch.object(workflows, "generate_study_plan", return_value="New plan") as generate:
            result = workflows.regenerate_plan(self.state, first)
            generate.assert_called_once_with(**parameters)
        self.assertNotEqual(result["plan_id"], first)
        self.assertEqual(self.state["view"], "plan")
        self.assertEqual(self.state["selected_plan_id"], result["plan_id"])
        self.assertEqual(plans.get_plan(first)["body"]["markdown"], "First plan")
        self.assertEqual(plans.list_plans()[0]["title"], "Exam preparation")
        self.assertTrue(plans.list_plans()[0]["pinned"])
        self.assertEqual([m["content"] for m in chats.list_messages(self.chat_id)], ["First plan", "New plan"])

    def test_plan_delete_requires_confirmation_and_preserves_chat_snapshot(self):
        with patch.object(workflows, "generate_study_plan", return_value="Saved plan body"):
            plan_id = workflows.run_tool(self.state, "plan", {})["plan_id"]
        workflows.open_plan_view(self.state)
        workflows.request_destructive_action(self.state, "plan", "Saved plan", plan_id)
        token = self.state["destructive_confirmation"]["token"]
        self.assertEqual(len(plans.list_plans()), 1)
        workflows.cancel_destructive_action(self.state, token)
        self.assertEqual(len(plans.list_plans()), 1)
        workflows.request_destructive_action(self.state, "plan", "Saved plan", plan_id)
        self.assertTrue(workflows.confirm_destructive_action(self.state, self.state["destructive_confirmation"]["token"]))
        self.assertEqual(plans.list_plans(), [])
        self.assertNotIn("selected_plan_id", self.state)
        self.assertEqual(chats.list_messages(self.chat_id)[0]["content"], "Saved plan body")
        workflows.open_tools(self.state)
        self.assertTrue(self.state["tools"])
        self.assertEqual(self.state["view"], "chat")

    def test_open_plan_view_chooses_pinned_or_latest_and_handles_empty(self):
        workflows.open_plan_view(self.state)
        self.assertEqual(self.state["view"], "plan")
        self.assertNotIn("selected_plan_id", self.state)
        first, latest = self.create_plan("First"), self.create_plan("Latest")
        workflows.open_plan_view(self.state)
        self.assertEqual(self.state["selected_plan_id"], latest)
        plans.pin_plan(first)
        workflows.open_plan_view(self.state)
        self.assertEqual(self.state["selected_plan_id"], first)
        plans.delete_plan(first)
        workflows.initialize_plan_selection(self.state, plans.list_plans())
        self.assertEqual(self.state["selected_plan_id"], latest)
        workflows.open_chat_view(self.state)
        self.assertEqual(self.state["view"], "chat")

    def test_generation_failure_does_not_append_or_save_incomplete_results(self):
        with patch.object(workflows, "generate_study_plan", side_effect=RuntimeError("Unavailable")):
            with self.assertRaises(RuntimeError):
                workflows.run_tool(self.state, "plan", {})
        with patch.object(workflows, "generate_study_plan", return_value="No documents are indexed. Upload notes before generating a study plan."):
            with self.assertRaises(ValueError):
                workflows.run_tool(self.state, "plan", {})
        self.assertEqual(plans.list_plans(), [])
        self.assertEqual(chats.list_messages(self.chat_id), [])

    def test_ui_renders_saved_plan_and_quiz_in_message_flow(self):
        chats.add_message(self.chat_id, "assistant", "Earlier text")
        with patch.object(workflows, "generate_study_plan", return_value="Saved plan"):
            workflows.run_tool(self.state, "plan", {})
        with patch.object(workflows, "generate_quiz", return_value=QUIZ):
            workflows.run_tool(self.state, "quiz", {})
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        names = {"show_chat_message", "show_quiz_result", "show_message_pin"}
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name == "show_tool_result" for node in tree.body))
        ui = MagicMock()
        ui.session_state = self.state
        ui.button.return_value = False
        namespace = {"st": ui, "flow": workflows, "grade_quiz": actions.grade_quiz,
                     "show_answer": Mock(), "show_sources": Mock(), "safe_error": str, "run_callback": Mock()}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<chat-messages>", "exec"), namespace)
        for message in chats.list_messages(self.chat_id):
            namespace["show_chat_message"](message, self.chat_id)
        namespace["show_answer"].assert_called_once()
        self.assertIn("Saved plan", [call.args[0] for call in ui.markdown.call_args_list])
        self.assertEqual(ui.radio.call_count, 3)
        self.assertTrue(all(call.kwargs['index'] is None for call in ui.radio.call_args_list))
        self.assertEqual(len({call.kwargs['key'] for call in ui.radio.call_args_list}), 3)


class MessageKindMigrationTests(unittest.TestCase):
    def test_existing_rows_default_to_text_and_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary, workspaces.storage_context(temporary):
            connection = sqlite3.connect(Path(temporary) / "app.db")
            connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)")
            connection.executemany("INSERT INTO messages VALUES (?, 'old', ?, ?, '2026-10-20')", [
                (7, "user", "Earlier question"), (9, "assistant", "Earlier answer"),
            ])
            connection.commit()
            connection.close()
            db.init_db()
            db.init_db()
            chat_id = chats.list_chats()[0]["id"]
            messages = chats.list_messages(chat_id)
            self.assertEqual([m["kind"] for m in messages], ["text", "text"])
            self.assertEqual([m["id"] for m in messages], [7, 9])
            self.assertEqual([m["content"] for m in messages], ["Earlier question", "Earlier answer"])
            chats.add_message(chat_id, "assistant", json.dumps(QUIZ), kind="quiz")
            db.init_db()
            self.assertEqual(chats.list_messages(chat_id)[-1]["kind"], "quiz")
            self.assertEqual(plans.list_plans(), [])


if __name__ == "__main__":
    unittest.main()
