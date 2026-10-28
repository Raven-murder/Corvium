from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

with patch("dotenv.load_dotenv"):
    from src import actions, chats, db, ingest, library, memory, workflows, workspaces


class MemoryActionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        storage = workspaces.storage_context(self.path)
        storage.__enter__()
        self.addCleanup(storage.__exit__, None, None, None)
        db.init_db()

    def add_document(self, name, text, vector):
        with db.database_session(write=True) as connection:
            document_id = connection.execute(
                "INSERT INTO documents(name, uploaded_at, n_chunks) VALUES (?, '2026-10-28', 1)", (name,)
            ).lastrowid
            connection.execute(
                "INSERT INTO chunks(doc_id, page, text, embedding) VALUES (?, 1, ?, ?)",
                (document_id, text, db.embedding_to_blob(vector)),
            )
        return document_id

    def test_add_update_memory_preserves_identity_kind_and_embedding(self):
        first = np.ones((1, 4096), dtype=np.float32)
        second = np.full((1, 4096), 2, dtype=np.float32)
        with patch.object(memory, "embed_texts", return_value=first):
            memory_id = memory.add_memory("Weak at normalization", "weak_topic")
            self.assertEqual(memory.add_memory("Weak at normalization", "weak_topic"), memory_id)
        before = memory.list_memories()[0]
        with patch.object(memory, "embed_texts", return_value=second):
            self.assertTrue(memory.update_memory_text(memory_id, "Weak at transactions"))
        after = memory.list_memories()[0]
        self.assertEqual(after["id"], memory_id)
        self.assertEqual(after["kind"], "weak_topic")
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertEqual(after["text"], "Weak at transactions")
        with db.database_session() as connection:
            blob = connection.execute("SELECT embedding FROM memories WHERE id = ?", (memory_id,)).fetchone()[0]
            np.testing.assert_array_equal(db.embedding_from_blob(blob), second[0])
        with patch.object(memory, "embed_texts") as embed:
            self.assertFalse(memory.update_memory_text(memory_id, "Weak at transactions"))
            embed.assert_not_called()

    def test_memory_validation_and_failed_update_preserve_data(self):
        with patch.object(memory, "embed_texts", return_value=np.ones((1, 4096), dtype=np.float32)):
            memory_id = memory.add_memory("Exam on 5 Nov", "deadline")
        before = memory.list_memories()
        for text, kind in (("", "fact"), ("Text", "invalid")):
            with self.assertRaises(ValueError):
                memory.add_memory(text, kind)
        for memory_id_value, text in ((memory_id, " "), (999, "A change")):
            with self.assertRaises(ValueError):
                memory.update_memory_text(memory_id_value, text)
        with patch.object(memory, "embed_texts", side_effect=RuntimeError("Unavailable")):
            with self.assertRaises(RuntimeError):
                memory.update_memory_text(memory_id, "Different date")
        self.assertEqual(memory.list_memories(), before)

    def test_deadline_and_weak_topic_defaults(self):
        memories = [
            {"kind": "deadline", "text": "Exam on 5 Nov"},
            {"kind": "deadline", "text": "Review on 2026-10-30"},
            {"kind": "weak_topic", "text": "Normalization"},
        ]
        self.assertEqual(actions.study_plan_defaults(memories, date(2026, 10, 28)), {"days": 2, "focus_topics": ["Normalization"]})
        self.assertEqual(actions.study_plan_defaults([], date(2026, 10, 28))["days"], 7)
        self.assertEqual(actions.study_plan_defaults([{"kind": "deadline", "text": "Exam 2026-10-28"}], date(2026, 10, 28))["days"], 1)
        self.assertEqual(actions.study_plan_defaults([{"kind": "deadline", "text": "Exam 2026-11-20"}], date(2026, 10, 28))["days"], 23)

    def test_selected_document_filter_runs_before_ranking(self):
        first_vector = np.zeros(4096, dtype=np.float32)
        first_vector[0] = 1
        other_vector = np.zeros(4096, dtype=np.float32)
        other_vector[1] = 1
        selected = self.add_document("selected.md", "Transactions are atomic.", other_vector)
        excluded = self.add_document("excluded.md", "Excluded high scoring text.", first_vector)
        with patch.object(ingest, "embed_texts", return_value=np.array([first_vector])):
            results = ingest.search("transactions", k=1, source_doc_ids=[selected])
        self.assertEqual([result["doc_id"] for result in results], [selected])
        self.assertNotEqual(results[0]["doc_id"], excluded)
        self.assertEqual([item["doc_id"] for item in actions._sample_chunks(source_doc_ids=[selected])], [selected])
        with patch.object(ingest, "embed_texts") as embed:
            self.assertEqual(ingest.search("transactions", source_doc_ids=[]), [])
            embed.assert_not_called()
        self.assertEqual(actions._sample_chunks(source_doc_ids=[]), [])

    def test_plan_focus_and_sources_are_optional_and_restricted(self):
        vector = np.ones(4096, dtype=np.float32)
        selected = self.add_document("selected.md", "Transactions are atomic.", vector)
        self.add_document("excluded.md", "Excluded passage.", vector)
        content = json.dumps({"items": ["Review atomicity [selected.md, p.1]."] * 2, "notices": []})
        with patch.object(actions, "_complete", return_value=content) as complete:
            result = actions.generate_study_plan(2, focus_topics=["Atomicity"], source_doc_ids=[selected])
        prompt = complete.call_args.args[1]
        self.assertIn('Selected focus topics: ["Atomicity"]', prompt)
        self.assertIn("Transactions are atomic.", prompt)
        self.assertNotIn("Excluded passage.", prompt)
        self.assertEqual(result.count("[selected.md, p.1]"), 2)
        with patch.object(actions, "_complete", return_value=content) as complete:
            actions.generate_study_plan()
            self.assertNotIn("Selected focus topics", complete.call_args.args[1])
        with patch.object(actions, "_complete") as complete:
            self.assertIn("No documents", actions.generate_study_plan(source_doc_ids=[]))
            complete.assert_not_called()

    def test_quiz_difficulty_and_selected_source_retrieval(self):
        vector = np.ones(4096, dtype=np.float32)
        selected = self.add_document("selected.md", "Atomicity groups updates.", vector)
        self.add_document("excluded.md", "Excluded passage.", vector)
        quiz = [{"question": f"Question {i}", "options": ["A", "B", "C", "D"], "answer_index": 2,
                 "explanation": "Atomicity groups updates.", "source_doc": "selected.md", "source_page": 1} for i in range(3)]
        with patch.object(actions, "_complete", return_value=json.dumps(quiz)) as complete, patch.object(ingest, "embed_texts", return_value=np.ones((1,4096), dtype=np.float32)):
            result = actions.generate_quiz(3, topic="Atomicity", difficulty="hard", source_doc_ids=[selected])
        self.assertEqual(actions.grade_quiz(result, [2, 2, 0]), (2, [True, True, False]))
        prompt = complete.call_args.args[1]
        self.assertIn("Difficulty: hard", prompt)
        self.assertNotIn("Excluded passage.", prompt)
        with patch.object(actions, "_complete", return_value=json.dumps(quiz)) as complete:
            actions.generate_quiz(3)
            self.assertNotIn("Difficulty:", complete.call_args.args[1])
        with patch.object(actions, "_complete") as complete:
            with self.assertRaises(ValueError):
                actions.generate_quiz(source_doc_ids=[])
            complete.assert_not_called()
        with self.assertRaises(ValueError):
            actions.generate_quiz(difficulty="unknown")
        with self.assertRaises(ValueError):
            actions.generate_study_plan(source_doc_ids=["1"])

    def test_weak_topic_retrieval_keeps_selected_sources(self):
        chunk = {"doc_id": 1, "doc_name": "selected.md", "page": 1, "text": "Atomicity"}
        with patch.object(actions, "_sample_chunks", return_value=[chunk]), patch.object(actions, "list_memories", return_value=[{"kind": "weak_topic", "text": "Atomicity"}]), patch.object(actions, "search", return_value=[chunk]) as search, patch.object(actions, "_complete", return_value="[]"):
            with self.assertRaises(ValueError):
                actions.generate_quiz(3, source_doc_ids=[1])
        self.assertEqual(search.call_args.kwargs["source_doc_ids"], [1])

    def test_moved_chat_workflow_starts_empty_once_and_confirms_deletion(self):
        old = chats.create_chat()
        chats.add_message(old, "user", "Earlier question")
        state = {}
        workflows.initialize_chat_selection(state, chats.list_chats())
        fresh = state["selected_chat_id"]
        self.assertNotEqual(fresh, old)
        workflows.initialize_chat_selection(state, chats.list_chats())
        self.assertEqual(len(chats.list_chats()), 2)
        state.pop("selected_chat_id")
        workflows.initialize_chat_selection(state, chats.list_chats())
        self.assertEqual(state["selected_chat_id"], fresh)
        workflows.delete_chat_from_sidebar(state, fresh)
        self.assertEqual(len(chats.list_chats()), 2)
        workflows.delete_chat_from_sidebar(state, fresh)
        self.assertEqual(state["selected_chat_id"], old)

    def test_workflows_focus_results_and_image_groups(self):
        state = {"new_focus_topic": "Atomicity", "plan_focus_topics": ["Normalization"]}
        workflows.add_focus_topic(state)
        self.assertEqual(state["plan_focus_topics"], ["Normalization", "Atomicity"])
        with patch.object(workflows, "generate_study_plan", return_value="Plan") as plan:
            workflows.run_tool(state, "plan", {"days": 2, "focus_topics": ["Atomicity"], "source_doc_ids": [1]})
            self.assertEqual(plan.call_args.kwargs["source_doc_ids"], [1])
        self.assertEqual(state["view"], "chat")
        self.assertEqual(chats.list_messages(state["selected_chat_id"])[-1]["kind"], "plan")
        groups = library.group_images_by_type([{"name": "a.jpeg"}, {"name": "b.png"}, {"name": "c.webp"}])
        self.assertEqual(list(groups), ["PNG", "JPG", "WEBP"])


if __name__ == "__main__":
    unittest.main()
