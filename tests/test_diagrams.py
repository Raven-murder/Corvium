import ast
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

with patch("dotenv.load_dotenv"):
    from src import chats, config, db, diagrams, qa, usage, workflows, workspaces


FLOW = {"type": "flowchart", "title": "Transactions", "nodes": [
    {"id": "A", "label": "Begin transaction", "kind": "start"},
    {"id": "B", "label": "Changes succeed?", "kind": "decision"},
    {"id": "C", "label": "Commit", "kind": "end"}],
    "edges": [{"from": "A", "to": "B"}, {"from": "B", "to": "C", "label": "Yes"}]}
FLOWCHART = json.dumps(FLOW)
MINDMAP = {"type": "mindmap", "title": "Transactions", "root": {"label": "Transactions", "children": [
    {"label": "Atomicity", "children": []}, {"label": "Isolation", "children": []}]}}

SOURCE = {"doc_id": 3, "doc_name": "Transactions", "page": 2,
          "text": "Begin a transaction. Commit if its changes succeed."}


def response(code, finish_reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=code), finish_reason=finish_reason,
    )])


class DiagramSanitizerTests(unittest.TestCase):
    def test_strips_json_fences_and_preserves_labels_decisions_and_edges(self):
        spec = json.loads(FLOWCHART)
        spec['nodes'][0]['label'] = 'Begin "student transaction"'
        cleaned = diagrams.sanitize_diagram('```json\n' + json.dumps(spec) + '\n```')
        self.assertEqual(cleaned['nodes'][0]['label'], spec['nodes'][0]['label'])
        self.assertEqual(cleaned['nodes'][1]['kind'], 'decision')
        self.assertEqual(cleaned['edges'], FLOW['edges'])
        self.assertEqual(cleaned, diagrams.sanitize_diagram(cleaned))

    def test_accepts_flowchart_and_mindmap_specs_and_rejects_removed_type(self):
        for spec, kind in ((FLOW, 'flowchart'), (MINDMAP, 'mind map')):
            with self.subTest(kind=kind):
                cleaned = diagrams.sanitize_diagram(spec, kind=kind)
                self.assertTrue(diagrams.validate_diagram(cleaned, kind=kind))
                self.assertEqual(cleaned, diagrams.sanitize_diagram(json.dumps(cleaned), kind=kind))
        self.assertFalse(diagrams.validate_diagram({'type': 'sequence', 'title': 'Transactions'}))

    def test_caps_nodes_for_both_json_diagram_types(self):
        for count in (25, 26):
            spec = {'type': 'flowchart', 'title': 'Transactions',
                    'nodes': [{'id': f'a{i}', 'label': f'Step {i}', 'kind': 'process'} for i in range(count)],
                    'edges': [{'from': f'a{i}', 'to': f'a{i + 1}'} for i in range(count - 1)]}
            self.assertEqual(diagrams.validate_diagram(spec), count <= 25)
        for count in (30, 31):
            spec = {'type': 'mindmap', 'title': 'Transactions', 'root': {'label': 'Transactions', 'children': [
                {'label': f'Step {i}', 'children': []} for i in range(count - 1)]}}
            self.assertEqual(diagrams.validate_diagram(spec), count <= 30)

    def test_preserves_json_edges_and_label_quotes(self):
        spec = json.loads(FLOWCHART)
        spec['edges'][1]['label'] = 'If "successful"'
        cleaned = diagrams.sanitize_diagram(spec)
        self.assertEqual(cleaned['edges'], spec['edges'])
        self.assertEqual(diagrams.sanitize_diagram(json.dumps(cleaned)), cleaned)

    def test_flowcharts_require_edges_no_isolated_nodes_and_connected_groups(self):
        for edges, problem in (([], 'no edges'), ([{'from': 'A', 'to': 'B'}], 'at least 2 edges'),
                               ([{'from': 'A', 'to': 'B'}, {'from': 'B', 'to': 'A'}], 'isolated nodes')):
            spec = {**FLOW, 'edges': edges}
            with self.subTest(problem=problem):
                self.assertFalse(diagrams.validate_diagram(spec))
                with self.assertRaisesRegex(ValueError, problem):
                    diagrams.sanitize_diagram(spec)
        spec = {**FLOW, 'edges': [{'from': 'A', 'to': 'unknown'}]}
        self.assertFalse(diagrams.validate_diagram(spec))

    def test_flowchart_renderer_always_uses_top_to_bottom(self):
        from unittest.mock import MagicMock
        for count in (2, 5, 6, 7, 8, 25):
            spec = {'type': 'flowchart', 'title': 'Transactions',
                    'nodes': [{'id': f'a{i}', 'label': f'Step {i}', 'kind': 'process'} for i in range(count)],
                    'edges': [{'from': f'a{i}', 'to': f'a{i + 1}'} for i in range(count - 1)]}
            graph = MagicMock()
            module = SimpleNamespace(Digraph=Mock(return_value=graph), escape=lambda label: label)
            with patch.dict('sys.modules', {'graphviz': module}):
                diagrams._build_graph(diagrams.sanitize_diagram(spec))
            self.assertEqual(graph.attr.call_args_list[0].kwargs['rankdir'], 'TB')
            self.assertEqual(graph.edge.call_count, count - 1)

    def test_rejects_bad_json_wrong_types_and_unexpected_fields(self):
        for invalid in ('Not JSON', 'null', '[]', 'Here is your diagram: ' + FLOWCHART,
                        json.dumps({**FLOW, 'style': 'untrusted'}), '{"type":"flowchart","type":"mindmap"}'):
            self.assertFalse(diagrams.validate_diagram(invalid))
        self.assertFalse(diagrams.validate_diagram(FLOWCHART, kind='mind map'))
        self.assertFalse(diagrams.validate_diagram(None))

    def test_labels_are_bounded_and_code_formats_are_rejected(self):
        self.assertFalse(diagrams.validate_diagram('digraph Notes { A -> B; }'))
        spec = json.loads(FLOWCHART)
        spec['nodes'][0]['label'] = 'X' * 61
        self.assertFalse(diagrams.validate_diagram(spec))
        self.assertFalse(diagrams.validate_diagram({**MINDMAP, 'root': {'label': 'A', 'children': [
            {'label': 'B', 'children': [{'label': 'C', 'children': [{'label': 'D', 'children': []}]}]}]}}))

    def test_detects_requested_diagram_kind(self):
        for question, expected in (
            ('Draw a flow chart of commits', 'flowchart'), ('FLOWCHART for recovery', 'flowchart'),
            ('Make a mind map', 'mind map'), ('Make a mindmap', 'mind map'),
            ('A sequence diagram of commits', 'flowchart'), ('Draw a diagram', 'flowchart'),
            ('Explain transactions', None),
        ):
            with self.subTest(question=question):
                self.assertEqual(diagrams.diagram_request_kind(question), expected)


class DiagramGenerationTests(unittest.TestCase):
    def setUp(self):
        self.complete = Mock(return_value=response(FLOWCHART))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.complete)))
        for name, value in (('get_client', client), ('search', [SOURCE]),
                            ('reserve_workspace_action', None)):
            mock = patch.object(diagrams, name, return_value=value)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)

    def test_uses_configured_model_retrieval_filter_and_tool_limit(self):
        result = diagrams.generate_diagram('Diagram the transaction', 'flowchart', doc_ids=[3])
        self.search.assert_called_once_with('Diagram the transaction', k=8, source_doc_ids=[3])
        self.reserve_workspace_action.assert_called_once_with('tool')
        call = self.complete.call_args.kwargs
        self.assertEqual(call['model'], config.CHAT_MODEL)
        self.assertGreaterEqual(call['max_tokens'], 4000)
        self.assertIn('Use ONLY facts and relationships', call['messages'][0]['content'])
        self.assertIn(SOURCE['text'], call['messages'][1]['content'])
        self.assertIn('[Transactions, p.2]', result['caption'])
        self.assertEqual(result['sources'], [SOURCE])
        self.assertEqual(diagrams.parse_diagram_message(result['answer']), result['spec'])

    def test_retries_node_cap_failure_once_then_returns_valid_spec(self):
        oversized = {**FLOW, 'nodes': [{'id': f'a{i}', 'label': f'Step {i}', 'kind': 'process'} for i in range(26)]}
        self.complete.side_effect = [response(json.dumps(oversized)), response(FLOWCHART)]
        result = diagrams.generate_diagram('Diagram transactions', 'flowchart')
        self.assertIsNotNone(result['spec'])
        self.assertEqual(self.complete.call_count, 2)
        self.assertIn('25 nodes', self.complete.call_args.kwargs['messages'][1]['content'])

    def test_invalid_json_empty_and_truncated_responses_fail_after_one_retry(self):
        for invalid in (response(json.dumps({'type': 'pie', 'title': 'Transactions'})), response(''), response(FLOWCHART, 'length')):
            with self.subTest(invalid=invalid):
                self.complete.reset_mock(side_effect=True)
                self.complete.return_value = invalid
                result = diagrams.generate_diagram('Diagram transactions', 'flowchart')
                self.assertIsNone(result['spec'])
                self.assertEqual(result['answer'], diagrams.FAILURE)
                self.assertEqual(self.complete.call_count, 2)

    def test_model_edges_survive_validation_and_generation_prompt_requires_them(self):
        result = diagrams.generate_diagram('Diagram transactions', 'flowchart')
        self.assertEqual(result['spec']['edges'], FLOW['edges'])
        prompt = self.complete.call_args.kwargs['messages'][0]['content']
        self.assertIn('ONLY a JSON', prompt)
        self.assertIn('nodes-1 edges', prompt)
        self.assertIn('existing node IDs', prompt)
        self.assertEqual(self.complete.call_count, 1)

    def test_edgeless_model_output_retries_with_specific_problem(self):
        invalid = json.dumps({**FLOW, 'edges': []})
        self.complete.side_effect = [response(invalid), response(FLOWCHART)]
        result = diagrams.generate_diagram('Diagram transactions', 'flowchart')
        self.assertIsNotNone(result['spec'])
        self.assertEqual(self.complete.call_count, 2)
        self.assertIn('Flowchart has no edges', self.complete.call_args.kwargs['messages'][1]['content'])
        self.complete.reset_mock(side_effect=True)
        self.complete.return_value = response(invalid)
        self.assertEqual(diagrams.generate_diagram('Diagram transactions', 'flowchart')['answer'], diagrams.FAILURE)
        self.assertEqual(self.complete.call_count, 2)

    def test_no_passages_never_calls_generation_model(self):
        self.search.return_value = []
        result = diagrams.generate_diagram('Diagram transactions', 'flowchart', doc_ids=[])
        self.assertIsNone(result['spec'])
        self.search.assert_called_once_with('Diagram transactions', k=8, source_doc_ids=[])
        self.complete.assert_not_called()

    def test_model_errors_are_friendly_and_usage_limits_are_not_retried(self):
        self.complete.side_effect = RuntimeError('Model unavailable')
        self.assertIsNone(diagrams.generate_diagram('Diagram transactions', 'flowchart')['spec'])
        self.assertEqual(self.complete.call_count, 2)
        self.complete.reset_mock(side_effect=True)
        self.complete.side_effect = usage.UsageLimitError('Please try again later.')
        with self.assertRaises(usage.UsageLimitError):
            diagrams.generate_diagram('Diagram transactions', 'flowchart')
        self.assertEqual(self.complete.call_count, 1)
        self.reserve_workspace_action.side_effect = usage.UsageLimitError('Please try again later.')
        self.complete.reset_mock(side_effect=True)
        self.search.reset_mock()
        with self.assertRaises(usage.UsageLimitError):
            diagrams.generate_diagram('Diagram transactions', 'flowchart')
        self.search.assert_not_called()
        self.complete.assert_not_called()

    def test_generation_always_returns_json_for_server_rendering(self):
        result = diagrams.generate_diagram('Diagram transactions', 'flowchart')
        self.assertEqual(result['language'], 'json')
        self.assertTrue(diagrams.validate_diagram(result['answer']))
        self.assertEqual(result['spec'], FLOW)
        self.assertIn('never code', self.complete.call_args.kwargs['messages'][0]['content'])

    def test_mindmap_generation_uses_json_and_removed_type_is_rejected(self):
        self.complete.return_value = response(json.dumps(MINDMAP))
        result = diagrams.generate_diagram('Transaction notes', 'mind map')
        self.assertEqual(result['spec'], MINDMAP)
        self.assertIn('30 nodes', self.complete.call_args.kwargs['messages'][0]['content'])
        with self.assertRaises(ValueError):
            diagrams.generate_diagram('Transaction notes', 'sequence')


class DiagramIntegrationTests(unittest.TestCase):
    def test_chat_stores_json_spec_and_sources_for_history(self):
        result = {'answer': FLOWCHART, 'sources': [SOURCE], 'spec': FLOW}
        with tempfile.TemporaryDirectory() as root, workspaces.storage_context(Path(root)):
            db.init_db()
            chat_id = chats.create_chat()
            with patch.object(qa, 'generate_diagram', return_value=result) as generate, patch.object(qa, '_answer_question') as text_answer:
                self.assertEqual(qa.answer_question('Show a flow chart', chat_id=chat_id), result)
                generate.assert_called_once_with('Show a flow chart', 'flowchart')
                text_answer.assert_not_called()
            history = chats.list_messages(chat_id)
            self.assertEqual([item['role'] for item in history], ['user', 'assistant'])
            self.assertEqual(history[-1]['sources'], [SOURCE])
            self.assertEqual(history[-1]['kind'], 'diagram')
            self.assertEqual(diagrams.parse_diagram_message(history[-1]['content']), FLOW)

    def test_diagram_tool_passes_parameters_and_saves_result(self):
        with tempfile.TemporaryDirectory() as root, workspaces.storage_context(Path(root)):
            chat_id = chats.create_chat()
            state = {'selected_chat_id': chat_id}
            parameters = {'question': 'Transactions', 'kind': 'mind map', 'doc_ids': [3]}
            result = {'answer': json.dumps(MINDMAP), 'spec': MINDMAP, 'sources': [SOURCE]}
            with patch.object(workflows, 'generate_diagram', return_value=result) as generate:
                workflows.run_tool(state, 'diagram', parameters)
                generate.assert_called_once_with(**parameters)
            message = chats.list_messages(chat_id)[0]
            self.assertEqual(message['kind'], 'diagram')
            self.assertEqual(message['content'], result['answer'])
            self.assertEqual(state['view'], 'chat')

    def test_enlarged_diagram_state_validates_content_and_clears_on_chat_change(self):
        state = {'selected_chat_id': 7}
        workflows.open_diagram_view(state, FLOWCHART, 'chat_7_message_2')
        self.assertEqual(workflows.active_diagram_view(state)['message_key'], 'chat_7_message_2')
        self.assertEqual(workflows.active_diagram_view(state)['spec'], FLOW)
        workflows.close_diagram_view(state)
        self.assertIsNone(workflows.active_diagram_view(state))
        with self.assertRaises(ValueError):
            workflows.open_diagram_view(state, 'Not a diagram', 'invalid')
        workflows.open_diagram_view(state, FLOWCHART, 'chat_7_message_2')
        state['selected_chat_id'] = 8
        self.assertIsNone(workflows.active_diagram_view(state))
        self.assertNotIn('diagram_view', state)

    def test_missing_dot_binary_has_friendly_outline_fallback(self):
        self.assertEqual(diagrams.diagram_language(), 'json')
        with patch.object(diagrams.shutil, 'which', return_value=None):
            with self.assertRaises(diagrams.DiagramRenderError):
                diagrams.render_diagram_png(FLOW)
        outline = diagrams.diagram_outline(MINDMAP)
        self.assertIn('Transactions', outline)
        self.assertIn('  - Atomicity', outline)

    def test_history_rendering_uses_png_and_legacy_code_stays_collapsed(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text(encoding='utf-8'))
        nodes = [item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name in {'show_answer', 'show_diagram_chart'}]
        from unittest.mock import MagicMock
        ui = MagicMock()
        namespace = {'st': ui, 'parse_diagram_message': diagrams.parse_diagram_message,
                     'run_callback': Mock(), 'flow': workflows, 'render_diagram_png': Mock(return_value=b'png'),
                     'diagram_caption': diagrams.diagram_caption, 'diagram_outline': diagrams.diagram_outline,
                     'DiagramRenderError': diagrams.DiagramRenderError}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<diagram-ui>', 'exec'), namespace)
        for key in ('chat_1_message_2', 'chat_1_message_4'):
            namespace['show_answer'](FLOWCHART, key, 'diagram', [SOURCE])
        self.assertEqual(ui.image.call_count, 2)
        self.assertTrue(all(call.kwargs['width'] == 'stretch' for call in ui.image.call_args_list))
        self.assertEqual(ui.download_button.call_count, 2)
        self.assertEqual(ui.expander.call_count, 0)
        self.assertTrue(all(call.args[0] == 'Enlarge' for call in ui.button.call_args_list))
        self.assertEqual(len(set(call.kwargs['key'] for call in ui.button.call_args_list)), 2)
        namespace['show_answer']('```mermaid\nflowchart TD\n A --> B\n```', 'older', 'diagram')
        self.assertFalse(ui.expander.call_args.kwargs['expanded'])
        ui.code.assert_called_once()
        namespace['show_answer']('Ordinary answer', 'text')
        ui.markdown.assert_called_once_with('Ordinary answer')


if __name__ == '__main__':
    unittest.main()
