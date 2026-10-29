import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

with patch("dotenv.load_dotenv"):
    from src import db, sources, workspaces, viewer


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

    def test_sources_buttons_open_one_docked_panel_and_hide_missing_downloads(self):
        source = self.document("notes.pdf", "Notes")
        missing = self.document("missing.md", "Missing")
        (self.folder / "files" / "missing.md").unlink()
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"show_sources", "show_sources_panel", "show_source_groups"}]
        ui = MagicMock()
        ui.session_state = {"selected_chat_id": 1}
        ui.columns.side_effect = lambda widths: [ui] * len(widths)
        namespace = {"st": ui, "group_answer_sources": sources.group_answer_sources,
                     "original_download": sources.original_download, "run_callback": Mock(),
                     "open_sources_panel": sources.open_sources_panel, "active_sources_panel": sources.active_sources_panel,
                     "sources_panel_dock": sources.sources_panel_dock, "dock_sources_panel": sources.dock_sources_panel,
                     "close_sources_panel": sources.close_sources_panel, "doc_viewer": viewer}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<sources-ui>", "exec"), namespace)
        for message_key in ("chat_1_message_2", "chat_1_message_4"):
            namespace["show_sources"]([source, missing], "Answer [Notes, p.1] [Missing, p.1]", message_key)
        buttons = list(ui.button.call_args_list)
        self.assertEqual(len(buttons), 2)
        self.assertTrue(all(call.args[0] == "Sources" for call in buttons))
        action, *args = buttons[-1].kwargs['args']
        action(ui.session_state, *args)
        namespace["show_sources_panel"]()
        ui.popover.assert_not_called()
        self.assertEqual(ui.session_state['sources_panel']['message_key'], 'chat_1_message_4')
        self.assertIn('sources_panel_right', [call.kwargs['key'] for call in ui.container.call_args_list])
        self.assertEqual(ui.download_button.call_count, 1)
        self.assertEqual(ui.download_button.call_args.kwargs['file_name'], 'notes.pdf')
        self.assertEqual(ui.expander.call_count, 2)
        self.assertTrue(all(call.args[0].startswith('• **') for call in ui.expander.call_args_list))
        keys = [call.kwargs['key'] for call in ui.button.call_args_list + ui.expander.call_args_list]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertFalse(any('score' in str(call.args[0]).lower() for call in ui.method_calls if call.args))
        self.assertEqual({call.kwargs['key'] for call in ui.button.call_args_list[2:]},
                         {'sources_dock_left', 'sources_dock_right', 'sources_dock_bottom', 'close_sources_panel',
                          *[f"source_open_chat_1_message_4_{group['key']}_0" for group in sources.group_answer_sources(
                              [source, missing], "Answer [Notes, p.1] [Missing, p.1]")]})

    def test_panel_replaces_previous_message_and_remembers_dock_after_close(self):
        state = {'selected_chat_id': 1}
        sources.open_sources_panel(state, [{'doc_name': 'First'}], 'First answer', 'first')
        sources.dock_sources_panel(state, 'left')
        sources.open_sources_panel(state, [{'doc_name': 'Second'}], 'Second answer', 'second')
        self.assertEqual(sources.active_sources_panel(state)['message_key'], 'second')
        self.assertEqual(sources.sources_panel_dock(state), 'left')
        sources.close_sources_panel(state)
        self.assertIsNone(sources.active_sources_panel(state))
        self.assertEqual(sources.sources_panel_dock(state), 'left')
        for dock in ('bottom', 'right'):
            sources.dock_sources_panel(state, dock)
            self.assertEqual(sources.sources_panel_dock(state), dock)
        with self.assertRaises(ValueError):
            sources.dock_sources_panel(state, 'invalid')
        self.assertEqual(sources.sources_panel_dock(state), 'right')
        state['sources_dock'] = 'invalid'
        self.assertEqual(sources.sources_panel_dock(state), 'right')

    def test_panel_closes_when_active_chat_changes(self):
        state = {'selected_chat_id': 1}
        sources.open_sources_panel(state, [], 'Answer', 'first')
        state['selected_chat_id'] = 2
        self.assertIsNone(sources.active_sources_panel(state))
        self.assertNotIn('sources_panel', state)


if __name__ == "__main__":
    unittest.main()
