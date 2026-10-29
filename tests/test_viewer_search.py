import ast
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image
from pypdf import PdfWriter

with patch("dotenv.load_dotenv"):
    from src import db, ingest, library, search, sources, usage, viewer, workspaces


def pdf_bytes(width=100, pages=2):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=50)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class ViewerSearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        context = workspaces.storage_context(self.root)
        self.storage = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        db.init_db()

    def document(self, name, display=None, chunks=None, contents=None, folder_id=None):
        with db.database_session(write=True) as connection:
            doc_id = connection.execute(
                "INSERT INTO documents(name, display_name, uploaded_at, n_chunks, folder_id) VALUES (?, ?, '2026-10-28', ?, ?)",
                (name, display or name, len(chunks or []), folder_id),
            ).lastrowid
            for page, section, text, vector in chunks or []:
                connection.execute("INSERT INTO chunks(doc_id, page, section, text, embedding) VALUES (?, ?, ?, ?, ?)",
                                   (doc_id, page, section, text, db.embedding_to_blob(vector)))
            if contents is not None:
                db.put_file(connection, doc_id, name, contents)
        return doc_id

    def chunk(self, text, page=1, section=None, axis=0):
        vector = np.zeros(4096, dtype=np.float32)
        vector[axis] = 1
        return page, section, text, vector

    def test_combined_search_ranks_names_and_returns_two_best_passages_with_one_embedding(self):
        folder = library.create_folder("Databases")
        first = self.document("transactions.md", "Revision", [
            self.chunk("First transaction passage.", section="Atomicity"),
            self.chunk("Second transaction passage.", section="Commit"),
            self.chunk("Third transaction passage.", section="Recovery", axis=1),
        ], folder_id=folder)
        second = self.document("other.md", "Other notes", [self.chunk("A relevant passage.")])
        self.document("unrelated.md", chunks=[self.chunk("No semantic similarity.", axis=1)])
        vector = np.zeros((1, 4096), dtype=np.float32)
        vector[0, 0] = 1
        with patch.object(ingest, "embed_texts", return_value=vector) as embed, \
                patch.object(search, "reserve_workspace_action") as reserve:
            results = search.search_documents("transactions", self.storage)
        embed.assert_called_once_with(["transactions"], max_attempts=1)
        reserve.assert_called_once_with("tool")
        self.assertEqual([result["id"] for result in results], [first, second])
        self.assertEqual(len(results[0]["passages"]), 2)
        self.assertEqual(results[0]["folder_name"], "Databases")
        self.assertEqual(results[0]["locator"], {"page": 1, "section": "Atomicity"})
        self.assertGreater(results[0]["score"], results[1]["score"])
        self.assertIsNone(results.notice)

    def test_filename_and_display_name_fallback_survive_limits_and_api_failures(self):
        original = self.document("TRANSACTIONS.md", "Renamed notes", [self.chunk("Fallback passage.")])
        renamed = self.document("course.md", "Transaction revision", [self.chunk("Another passage.")])
        for error in (usage.UsageLimitError("Limit reached"), RuntimeError("Do not expose API details")):
            with self.subTest(error=type(error).__name__):
                with patch.object(search, "retrieve_passages", side_effect=error), patch.object(search, "reserve_workspace_action"):
                    results = search.search_documents("transaction", self.storage)
                self.assertEqual({result["id"] for result in results}, {original, renamed})
                self.assertIn("Filename matches are still shown", results.notice)
                self.assertNotIn("API details", results.notice)
        with patch.object(search, "reserve_workspace_action", side_effect=usage.UsageLimitError("Limit reached")), \
                patch.object(search, "retrieve_passages") as retrieve:
            results = search.search_documents("TRANSACTIONS", self.storage)
            retrieve.assert_not_called()
        self.assertEqual([result["id"] for result in results], [original])

    def test_failed_query_embedding_is_attempted_once_without_blocking_name_results(self):
        doc_id = self.document("notes.md", chunks=[self.chunk("A complete sentence.")])
        with patch.object(ingest, "get_client") as client, patch.object(ingest, "time") as clock:
            client.return_value.embeddings.create.side_effect = RuntimeError("Unavailable")
            results = search.search_documents("notes", self.storage)
            client.return_value.embeddings.create.assert_called_once()
            clock.sleep.assert_not_called()
        self.assertEqual([result["id"] for result in results], [doc_id])
        self.assertIsNotNone(results.notice)

    def test_search_previews_end_at_sentence_boundaries_and_filename_only_docs_open(self):
        text = "Complete first sentence. " + "long word " * 100 + "End."
        self.document("notes.md", chunks=[self.chunk(text)])
        no_text = self.document("notes.png")
        with patch.object(search, "retrieve_passages", side_effect=RuntimeError("Unavailable")):
            results = search.search_documents("notes", self.storage)
        self.assertEqual(results[0]["passages"][0]["text"], "Complete first sentence.")
        image = next(result for result in results if result["id"] == no_text)
        self.assertEqual(image["locator"]["page"], 1)
        self.assertEqual(image["passages"], [])

    def test_search_rejects_other_workspaces_and_does_not_repeat_on_reruns(self):
        self.document("notes.md")
        with self.assertRaises(workspaces.WorkspaceError):
            search.search_documents("notes", "../other")
        other = workspaces.Storage(self.root / "other")
        with self.assertRaises(workspaces.WorkspaceError):
            search.search_documents("notes", other)
        state = {"document_search_query": "notes"}
        with patch.object(search, "reserve_workspace_action") as reserve:
            search.queue_search(state)
            search.run_pending_search(state)
            search.run_pending_search(state)
            reserve.assert_called_once_with("tool")
        self.assertIsNotNone(search.search_results(state))
        with workspaces.storage_context(self.root / "other"):
            self.assertIsNone(search.search_results(state))

    def test_locators_restore_per_document_and_clamp_pages_and_slide_sections(self):
        pdf_id = self.document("scan.pdf", contents=pdf_bytes(pages=3))
        state = {}
        viewer.request_viewer(state, pdf_id, 99)
        self.assertEqual(state["viewer_request"]["locator"]["page"], 3)
        viewer.move_page(state, -1)
        viewer.close_viewer(state)
        viewer.request_viewer(state, pdf_id)
        self.assertEqual(state["viewer_request"]["locator"]["page"], 2)
        with patch.object(viewer.st, "session_state", state):
            viewer.open_viewer(pdf_id, "Page 1")
        self.assertEqual(state["viewer_request"]["locator"]["page"], 1)
        slide_id = self.document("slides.pptx", chunks=[
            self.chunk("First slide.", page=1, section="Atomicity"),
            self.chunk("Second slide.", page=2, section="Recovery"),
        ])
        viewer.request_viewer(state, slide_id, {"slide": 2})
        self.assertEqual(state["viewer_request"]["locator"], {"page": 2, "section": "Recovery"})
        viewer.request_viewer(state, slide_id, "Atomicity")
        self.assertEqual(state["viewer_request"]["locator"]["page"], 1)
        with workspaces.storage_context(self.root / "other"):
            self.assertIsNone(viewer.active_viewer(state))
        with self.assertRaises(ValueError):
            viewer.open_viewer("../scan.pdf")

    def test_pdf_rendering_cache_is_scoped_and_checks_original_before_cache(self):
        import pypdfium2
        viewer._cached_pdf_page.clear()
        doc_id = self.document("scan.pdf", contents=pdf_bytes())
        with patch.object(pypdfium2, "PdfDocument", wraps=pypdfium2.PdfDocument) as render:
            first = viewer.render_pdf_page(doc_id, 1)
            self.assertEqual(viewer.render_pdf_page(doc_id, 1), first)
            self.assertEqual(render.call_count, 1)
            with Image.open(BytesIO(first["png"])) as image:
                self.assertEqual(image.size, (160, 80))
            with db.database_session(write=True) as connection:
                db.put_file(connection, doc_id, "scan.pdf", pdf_bytes(width=120))
            self.assertNotEqual(viewer.render_pdf_page(doc_id, 1)["png"], first["png"])
            self.assertEqual(render.call_count, 2)
            with workspaces.storage_context(self.root / "other"):
                other_id = self.document_in_current_workspace("scan.pdf", pdf_bytes())
                viewer.render_pdf_page(other_id, 1)
            self.assertEqual(render.call_count, 3)
            with db.database_session(write=True) as connection:
                connection.execute("DELETE FROM files WHERE doc_id = ?", (doc_id,))
            self.assertIsNone(db.get_file_bytes(db.current_workspace_id(), doc_id))
            self.assertIsNone(viewer.render_pdf_page(doc_id, 1))
        self.assertEqual(first["page_count"], 2)

    def document_in_current_workspace(self, name, contents):
        with db.database_session(write=True) as connection:
            doc_id = connection.execute("INSERT INTO documents(name, uploaded_at, n_chunks) VALUES (?, '2026-10-28', 0)",
                                        (name,)).lastrowid
            db.put_file(connection, doc_id, name, contents)
        return doc_id

    def test_docx_pptx_html_use_stored_text_when_original_is_missing(self):
        for name, section, text, expected in (
            ("notes.docx", "Atomicity", "Commit both updates.", "Commit both updates."),
            ("slides.pptx", "Recovery", "Roll back failed changes.", "Roll back failed changes."),
            ("page.html", "<b>Atomicity</b>", "<script>hidden script</script><style>hidden CSS</style><p>Visible text.</p>", "Visible text."),
        ):
            with self.subTest(name=name):
                doc_id = self.document(name, chunks=[self.chunk(text, section=section)])
                data = viewer.load_document(doc_id)
                self.assertFalse(data["original_available"])
                self.assertIn("original file is unavailable", data["notice"])
                self.assertEqual(data["parts"][0]["text"], expected)
                self.assertNotIn("<", data["parts"][0]["text"])
                self.assertNotIn("<", data["parts"][0]["section"])
                self.assertNotIn("hidden", data["parts"][0]["text"])

    def test_plain_text_sections_and_overlapping_chunks_are_preserved(self):
        shared = "shared overlap of indexed passage windows"
        doc_id = self.document("notes.md", chunks=[
            self.chunk("Opening sentence. " + shared, section="Atomicity"),
            self.chunk(shared + " Closing sentence.", section="Atomicity"),
            self.chunk("Recovery sentence.", section="Recovery"),
        ])
        data = viewer.load_document(doc_id)
        self.assertEqual(len(data["parts"]), 2)
        self.assertEqual(data["parts"][0]["text"].count(shared), 1)
        self.assertEqual(viewer.selected_parts(data, "Recovery")[0]["text"], "Recovery sentence.")
        self.assertEqual(viewer.selected_part_index(data, "Recovery"), 1)

    def test_images_use_original_bytes_and_ocr_fallback(self):
        output = BytesIO()
        with Image.new("RGB", (30, 20), "white") as image:
            image.save(output, format="PNG")
        doc_id = self.document("scan.png", chunks=[self.chunk("OCR text from a scan.")], contents=output.getvalue())
        self.assertEqual(viewer.image_original(doc_id), output.getvalue())
        with db.database_session(write=True) as connection:
            connection.execute("DELETE FROM files WHERE doc_id = ?", (doc_id,))
        self.assertIsNone(db.get_file_bytes(db.current_workspace_id(), doc_id))
        self.assertIsNone(viewer.image_original(doc_id))
        self.assertEqual(viewer.load_document(doc_id)["parts"][0]["text"], "OCR text from a scan.")

    def test_viewer_ui_never_renders_raw_document_html(self):
        doc_id = self.document("page.html", chunks=[self.chunk("<p>Visible sentence.</p><script>hidden</script>", section="Heading")])
        data = viewer.load_document(doc_id)
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "show_viewer_parts")
        ui = MagicMock()
        namespace = {"st": ui, "doc_viewer": viewer}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<viewer-text>", "exec"), namespace)
        namespace["show_viewer_parts"](data, {"page": 1})
        ui.markdown.assert_not_called()
        self.assertIn("Visible sentence.", [call.args[0] for call in ui.text.call_args_list])

    def test_source_locators_include_document_id_and_section(self):
        doc_id = self.document("notes.docx", chunks=[self.chunk("Commit both updates.", section="Atomicity")])
        source = {"doc_id": doc_id, "doc_name": "notes.docx", "page": 1, "text": "Commit both updates."}
        group = sources.group_answer_sources([source], "Answer [notes.docx, p.1]")[0]
        self.assertEqual(group["doc_id"], doc_id)
        self.assertEqual(group["passages"][0]["locator"], {"page": 1, "section": "Atomicity"})


if __name__ == "__main__":
    unittest.main()
