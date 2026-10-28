import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

with patch("dotenv.load_dotenv"):
    from src import db, sources, workspaces


class SourceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        storage = workspaces.storage_context(self.folder)
        storage.__enter__()
        self.addCleanup(storage.__exit__, None, None, None)
        db.init_db()

    def document(self, name, display_name, section=None, text="A complete source sentence.", page=1):
        path = self.folder / "files" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"Original file contents")
        with db.database_session(write=True) as connection:
            document_id = connection.execute(
                "INSERT INTO documents(name, display_name, uploaded_at, n_chunks, stored_path) VALUES (?, ?, '2026-10-28', 1, ?)",
                (name, display_name, str(path)),
            ).lastrowid
            connection.execute(
                "INSERT INTO chunks(doc_id, page, text, embedding, section) VALUES (?, ?, ?, ?, ?)",
                (document_id, page, text, b"fixture-vector", section),
            )
        return {"doc_id": document_id, "doc_name": display_name, "page": page, "text": text, "score": 0.92}

    def test_groups_only_cited_documents_and_deduplicates_passages(self):
        first = self.document("notes.pdf", "Database notes", page=2)
        second = dict(first, page=1, text="A different sentence.")
        excluded = self.document("other.pdf", "Other notes")
        groups = sources.group_answer_sources([first, second, first, excluded], "Answer [Database notes, p.2] [Database notes, p.1]")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["doc_name"], "Database notes")
        self.assertEqual([part["heading"] for part in groups[0]["passages"]], ["Page 1", "Page 2"])
        self.assertNotIn("score", groups[0])
        self.assertTrue(all("score" not in passage for passage in groups[0]["passages"]))

    def test_uses_display_name_and_section_for_old_source_records(self):
        source = self.document("notes.md", "Before rename", section="Normalization")
        with db.database_session(write=True) as connection:
            connection.execute("UPDATE documents SET display_name = 'After rename' WHERE id = ?", (source["doc_id"],))
        group = sources.group_answer_sources([source], "Answer [Before rename, p.1]")[0]
        self.assertEqual(group["doc_name"], "After rename")
        self.assertEqual(group["passages"][0]["heading"], "Normalization")
        download = sources.original_download(group)
        self.assertEqual(download["data"], b"Original file contents")
        self.assertEqual(download["file_name"], "notes.md")

    def test_passage_labels_for_supported_kinds(self):
        for extension, section, expected in (
            ("pdf", "Ignored", "Page 3"), ("docx", "Ignored", "Page 3"),
            ("pptx", "Ignored", "Slide 3"), ("png", None, "Page 3"),
            ("md", "Atomicity", "Atomicity"), ("txt", "Recovery", "Recovery"),
            ("html", "Isolation", "Isolation"), ("txt", None, "Text"),
        ):
            with self.subTest(extension=extension, section=section):
                source = self.document(f"note-{section}.{extension}", f"Display {extension} {section}", section=section, page=3)
                group = sources.group_answer_sources([source], f"Answer [{source['doc_name']}, p.3]")[0]
                self.assertEqual(group["passages"][0]["heading"], expected)

    def test_preview_ends_at_a_sentence_and_never_exceeds_limit(self):
        self.assertEqual(sources.passage_preview("Short sentence."), "Short sentence.")
        text = "First sentence. " + "word " * 150 + "Ends later."
        self.assertEqual(sources.passage_preview(text), "First sentence.")
        text = "A" * 598 + ". Another sentence."
        self.assertEqual(len(sources.passage_preview(text)), 599)
        self.assertTrue(sources.passage_preview(text).endswith("."))
        self.assertEqual(sources.passage_preview("word " * 200), "")
        self.assertEqual(sources.passage_preview('He said "Done!" ' + "word " * 150), 'He said "Done!"')

    def test_missing_originals_and_deleted_documents_have_no_download(self):
        source = self.document("notes.md", "Notes")
        group = sources.group_answer_sources([source], "Answer [Notes, p.1]")[0]
        Path(group["stored_path"]).unlink()
        self.assertIsNone(sources.original_download(group))
        self.assertIsNone(sources.original_download({"stored_path": None}))
        with db.database_session(write=True) as connection:
            connection.execute("DELETE FROM documents WHERE id = ?", (source["doc_id"],))
        group = sources.group_answer_sources([source], "Answer [Notes, p.1]")[0]
        self.assertIsNone(group["stored_path"])
        self.assertEqual(group["passages"][0]["text"], source["text"])

    def test_legacy_sources_without_ids_resolve_original_filename(self):
        source = self.document("legacy.md", "Renamed legacy", section="Transactions")
        source.pop("doc_id")
        source["doc_name"] = "legacy.md"
        group = sources.group_answer_sources([source], "Answer [legacy.md, p.1]")[0]
        self.assertEqual(group["doc_name"], "Renamed legacy")
        self.assertEqual(group["passages"][0]["heading"], "Transactions")
        self.assertIsNotNone(sources.original_download(group))

    def test_popover_has_unique_message_keys_and_hides_missing_downloads(self):
        source = self.document("notes.pdf", "Notes")
        missing = self.document("missing.md", "Missing")
        (self.folder / "files" / "missing.md").unlink()
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "show_sources")

        class UI:
            def __init__(self):
                self.elements = []
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def popover(self, label, **kwargs):
                self.elements.append(("popover", label, kwargs))
                return self
            def expander(self, label, **kwargs):
                self.elements.append(("expander", label, kwargs))
                return self
            def columns(self, widths): return [self] * len(widths)
            def markdown(self, text): self.elements.append(("markdown", text, {}))
            def text(self, text): self.elements.append(("text", text, {}))
            def caption(self, text): self.elements.append(("caption", text, {}))
            def download_button(self, label, **kwargs): self.elements.append(("download", label, kwargs))
        ui = UI()
        namespace = {"st": ui, "group_answer_sources": sources.group_answer_sources, "original_download": sources.original_download}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<sources-ui>", "exec"), namespace)
        for message_key in ("chat_1_message_2", "chat_1_message_4"):
            namespace["show_sources"]([source, missing], "Answer [Notes, p.1] [Missing, p.1]", message_key)
        popovers = [item for item in ui.elements if item[0] == "popover"]
        self.assertEqual(len(popovers), 2)
        self.assertTrue(all(item[1] == "Sources" and item[2]["width"] == "stretch" for item in popovers))
        downloads = [item for item in ui.elements if item[0] == "download"]
        self.assertEqual(len(downloads), 2)
        self.assertTrue(all(item[2]["file_name"] == "notes.pdf" for item in downloads))
        keys = [item[2]["key"] for item in ui.elements if "key" in item[2]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertFalse(any("score" in item[1].lower() for item in ui.elements))
        self.assertTrue(all(item[1].startswith("• **") for item in ui.elements if item[0] == "expander"))


if __name__ == "__main__":
    unittest.main()
