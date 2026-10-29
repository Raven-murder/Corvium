"""Mocked plan checks; never import configuration or load .env."""

import ast
from datetime import date, timedelta
import json
from pathlib import Path
import re
import unittest


class StudyPlanTests(unittest.TestCase):
    def test_real_study_days_and_fallbacks(self):
        source = Path(__file__).resolve().parents[1] / "src" / "actions.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {"generate_study_plan", "_render_plan", "_valid_plan_task"}
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        chunks = [{"doc_name": "notes.pdf", "page": 1, "text": "Covered material."}]
        namespace = {
            "re": re, "json": json, "date": date, "timedelta": timedelta,
            "reserve_workspace_action": lambda kind: None,
            "TONE": "test", "_sample_chunks": lambda: chunks,
            "list_memories": lambda: [], "_context": lambda value: "notes",
            "_deadline_countdown": lambda memories, today:
                "Days remaining until the nearest deadline (2026-11-05): 23.",
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<mock-plan>", "exec"), namespace)
        good = "Review the covered material [notes.pdf, p.1]."
        for items in ([good] * 7, [good] * 6, [good] * 8,
                      ["Available notes do not cover normalization", "DBMS exam is on 5 Nov"] + [good] * 5):
            with self.subTest(items=items):
                calls = []

                def complete(system, prompt):
                    calls.append(prompt)
                    return json.dumps({"notices": ["Available notes do not cover normalization."], "items": items})

                namespace["_complete"] = complete
                result = namespace["generate_study_plan"](7)
                dated_lines = re.findall(r"^- \*\*(\d{4}-\d{2}-\d{2})\*\*: (.+)$", result, re.M)
                self.assertEqual([label for label, _ in dated_lines], [
                    (date.today() + timedelta(days=i)).isoformat() for i in range(7)
                ])
                self.assertEqual(len({label for label, _ in dated_lines}), 7)
                for _, task in dated_lines:
                    self.assertTrue(namespace["_valid_plan_task"](task, chunks))
                    self.assertIn("[notes.pdf, p.1]", task)
                    self.assertNotIn("exam is on", task)
                    self.assertNotIn("not cover", task)
                self.assertEqual(result.count("Available notes do not cover normalization."), 1)
                self.assertTrue(result.startswith("Available notes do not cover normalization."))
                self.assertIn("covers only the first 7 days", result)
                self.assertTrue(result.splitlines()[-1].startswith("Days remaining"))
                self.assertEqual(len(calls), 1 if items == [good] * 7 else 2)


if __name__ == "__main__":
    unittest.main()
