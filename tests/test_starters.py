from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

with patch("dotenv.load_dotenv"):
    from src import chats, workflows, workspaces


class StarterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = workspaces.storage_context(Path(temporary.name))
        storage.__enter__()
        self.addCleanup(storage.__exit__, None, None, None)

    def test_summary_is_queued_then_consumed_once(self):
        state = {"selected_chat_id": 7}
        with patch.object(workflows, "generate_study_plan") as plan, patch.object(workflows, "generate_quiz") as quiz:
            workflows.queue_starter(state, "summary")
            plan.assert_not_called()
            quiz.assert_not_called()
        self.assertEqual(workflows.consume_starter(state), ("Summarize my notes", None))
        self.assertEqual(workflows.consume_starter(state), (None, None))

    def test_tool_starters_call_existing_functions_with_default_parameters(self):
        for kind, function, result in (("plan", "generate_study_plan", "Study plan"), ("quiz", "generate_quiz", ["Question"])):
            with self.subTest(kind=kind):
                state = {"selected_chat_id": chats.create_chat(), "quiz_answers": {99: [1]}}
                workflows.queue_starter(state, kind)
                question, pending = workflows.consume_starter(state)
                self.assertIsNone(question)
                self.assertEqual(pending, (kind, {}))
                with patch.object(workflows, function, return_value=result) as generate:
                    workflows.run_tool(state, *pending)
                    generate.assert_called_once_with()
                self.assertEqual(state["view"], "chat")
                self.assertEqual(chats.list_messages(state["selected_chat_id"])[-1]["kind"], kind)
                self.assertEqual(workflows.consume_starter(state), (None, None))
                if kind == "quiz":
                    self.assertEqual(state["quiz_answers"], {99: [1]})

    def test_stale_or_unknown_starters_do_not_run(self):
        state = {"selected_chat_id": 7}
        workflows.queue_starter(state, "plan")
        state["selected_chat_id"] = 8
        self.assertEqual(workflows.consume_starter(state), (None, None))
        self.assertNotIn("starter_action", state)
        with self.assertRaises(ValueError):
            workflows.queue_starter(state, "unknown")
        self.assertNotIn("starter_action", state)


if __name__ == "__main__":
    unittest.main()
