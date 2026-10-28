import ast
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

with patch("dotenv.load_dotenv"):
    from src import chats, config, db, diagrams, qa, usage, workflows, workspaces


FLOWCHART = 'flowchart TD\n A[Begin transaction] --> B{Changes succeed?}\n B -->|Yes| C[Commit]'
SOURCE = {"doc_id": 3, "doc_name": "Transactions", "page": 2,
          "text": "Begin a transaction. Commit if its changes succeed."}


def response(code, finish_reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=code), finish_reason=finish_reason,
    )])


class DiagramSanitizerTests(unittest.TestCase):
    def test_strips_fences_and_quotes_and_preserves_decisions_and_edges(self):
        code = diagrams.sanitize_diagram(
            '```mermaid\nflowchart TD\n A["Begin transaction"] --> B{Changes succeed?}\n'
            ' B -->|"Yes"| C[Commit the \'students\' updates]\n```'
        )
        self.assertNotIn('```', code)
        self.assertNotIn('"', code)
        self.assertNotIn("'", code)
        self.assertIn('n2{Changes succeed?}', code)
        self.assertIn('n2 -->|Yes| n3', code)
        self.assertEqual(code, diagrams.sanitize_diagram(code))

    def test_accepts_graph_mindmap_and_sequence_headers(self):
        examples = (
            ('graph LR\n A[Read] --> B[Commit]', 'flowchart'),
            ('mindmap\n root((Transactions))\n   a[Atomicity]\n   b[Isolation]', 'mind map'),
            ('sequenceDiagram\n participant A as "Client"\n participant B as Database\n A->>B: "Commit"', 'sequence'),
        )
        for code, kind in examples:
            with self.subTest(kind=kind):
                cleaned = diagrams.sanitize_diagram(code, kind=kind)
                self.assertTrue(diagrams.validate_diagram(cleaned, kind=kind))
                self.assertNotIn('"', cleaned)
                self.assertEqual(cleaned, diagrams.sanitize_diagram(cleaned, kind=kind))

    def test_caps_explicit_and_implicit_nodes_for_every_diagram_type(self):
        for count in (25, 26):
            examples = (
                'flowchart TD\n' + '\n'.join(f'a{i}[Step {i}]' for i in range(count)) + '\n' + '\n'.join(f'a{i} --> a{i + 1}' for i in range(count - 1)),
                'flowchart TD\n' + ' --> '.join(f'a{i}' for i in range(count)),
                'mindmap\n root[Transactions]\n' + '\n'.join(f'   a{i}[Step {i}]' for i in range(count - 1)),
                'sequenceDiagram\n' + '\n'.join(f'a0->>a{i}: Update' for i in range(1, count)),
                'digraph Notes {\n' + '\n'.join(f'a{i} [label="Step {i}"];' for i in range(count)) + '\n}',
            )
            for code in examples:
                with self.subTest(count=count, header=code.splitlines()[0]):
                    language = 'dot' if code.startswith('digraph') else 'mermaid'
                    self.assertEqual(diagrams.validate_diagram(code, language), count <= 25)

    def test_preserves_all_edge_tokens_and_label_quotes(self):
        for arrow in ('-->', '---', '-.->', '==>', '<-->'):
            with self.subTest(arrow=arrow):
                code = diagrams.sanitize_diagram(f'flowchart TD\n A["Start"] {arrow}|"Next"| B[Finish]')
                self.assertIn(f'n1 {arrow}|Next| n2', code)
                self.assertNotIn('"', code)
                self.assertTrue(code.startswith('flowchart LR'))
                self.assertEqual(diagrams.sanitize_diagram(code), code)

    def test_flowcharts_require_edges_no_isolated_nodes_and_connected_groups(self):
        invalid = (
            ('flowchart TD\n A[Start]\n B[Finish]', 'no edges'),
            ('flowchart TD\n A --> B\n C --> D', 'at least 3 edges'),
            ('flowchart TD\n A --> B\n B --> A\n C[Isolated]', 'isolated nodes'),
            ('flowchart TD\n A --> B\n B --> A\n C --> D\n D --> C', 'disconnected groups'),
        )
        for code, problem in invalid:
            with self.subTest(problem=problem):
                self.assertFalse(diagrams.validate_diagram(code))
                with self.assertRaisesRegex(ValueError, problem):
                    diagrams.sanitize_diagram(code)

    def test_direction_uses_lr_only_up_to_five_nodes_without_removing_edges(self):
        for count, direction in ((2, 'LR'), (5, 'LR'), (6, 'TD'), (7, 'TD'), (8, 'TD'), (25, 'TD')):
            code = 'flowchart RL\n' + '\n'.join(f'A{i} --> A{i + 1}' for i in range(count - 1))
            cleaned = diagrams.sanitize_diagram(code)
            self.assertTrue(cleaned.startswith(f'flowchart {direction}'))
            self.assertEqual(cleaned.count('-->'), count - 1)

    def test_rejects_bad_headers_wrong_kinds_and_unsupported_directives(self):
        for code in (
            'Here is your diagram:\n' + FLOWCHART, 'pie\n "Commit": 1',
            'sequenceDiagramBroken\n A->>B: Commit', 'flowchartBad\n A --> B',
            '%%{init: {}}%%\n' + FLOWCHART, FLOWCHART + '\n click A "https://example.com"',
        ):
            with self.subTest(code=code):
                self.assertFalse(diagrams.validate_diagram(code))
        self.assertFalse(diagrams.validate_diagram(FLOWCHART, kind='sequence'))
        self.assertFalse(diagrams.validate_diagram(None))

    def test_dot_labels_are_sanitized_and_unsupported_attributes_rejected(self):
        code = diagrams.sanitize_diagram('digraph Notes { A [label="Begin \'transaction\'"]; A -> B; }', 'dot')
        self.assertIn('[label="Begin transaction"]', code)
        self.assertTrue(diagrams.validate_diagram(code, 'dot'))
        self.assertFalse(diagrams.validate_diagram('digraph { A [URL="https://example.com"]; }', 'dot'))

    def test_detects_requested_diagram_kind(self):
        for question, expected in (
            ('Draw a flow chart of commits', 'flowchart'), ('FLOWCHART for recovery', 'flowchart'),
            ('Make a mind map', 'mind map'), ('Make a mindmap', 'mind map'),
            ('A sequence diagram of commits', 'sequence'), ('Draw a diagram', 'flowchart'),
            ('Explain transactions', None),
        ):
            with self.subTest(question=question):
                self.assertEqual(diagrams.diagram_request_kind(question), expected)


class DiagramGenerationTests(unittest.TestCase):
    def setUp(self):
        self.complete = Mock(return_value=response(FLOWCHART))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.complete)))
        for name, value in (('get_client', client), ('search', [SOURCE]),
                            ('diagram_language', 'mermaid'), ('reserve_workspace_action', None)):
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
        self.assertIn('[Transactions, p.2]', result['answer'])
        self.assertEqual(result['sources'], [SOURCE])
        self.assertEqual(diagrams.parse_diagram_message(result['answer'])['code'], result['code'])

    def test_retries_node_cap_failure_once_then_returns_valid_code(self):
        oversized = 'flowchart TD\n' + '\n'.join(f'a{i}[Step {i}]' for i in range(26))
        self.complete.side_effect = [response(oversized), response(FLOWCHART)]
        result = diagrams.generate_diagram('Diagram transactions', 'flowchart')
        self.assertIsNotNone(result['code'])
        self.assertEqual(self.complete.call_count, 2)
        self.assertIn('Correction:', self.complete.call_args.kwargs['messages'][1]['content'])

    def test_bad_header_empty_and_truncated_responses_fail_after_one_retry(self):
        for invalid in (response('pie\n Commit: 1'), response(''), response(FLOWCHART, 'length')):
            with self.subTest(invalid=invalid):
                self.complete.reset_mock(side_effect=True)
                self.complete.return_value = invalid
                result = diagrams.generate_diagram('Diagram transactions', 'flowchart')
                self.assertIsNone(result['code'])
                self.assertEqual(result['answer'], diagrams.FAILURE)
                self.assertEqual(self.complete.call_count, 2)

    def test_model_edges_survive_sanitization_and_generation_prompt_requires_them(self):
        code = 'flowchart TD\n A["Begin"] -->|"Then"| B[Update]\n B --- C[Check]\n C -.-> D[Retry]\n D ==> E[Commit]'
        self.complete.return_value = response(code)
        result = diagrams.generate_diagram('Diagram transactions', 'flowchart')
        for edge in ('n1 -->|Then| n2', 'n2 --- n3', 'n3 -.-> n4', 'n4 ==> n5'):
            self.assertIn(edge, result['code'])
        prompt = self.complete.call_args.kwargs['messages'][0]['content']
        self.assertIn('one edge per line', prompt)
        self.assertIn('same node IDs', prompt)
        self.assertIn('A --> B\n B -->|Yes| C', prompt)
        self.assertEqual(self.complete.call_count, 1)

    def test_edgeless_model_output_retries_with_specific_problem(self):
        self.complete.side_effect = [response('flowchart TD\n A[Begin]\n B[Commit]'), response(FLOWCHART)]
        result = diagrams.generate_diagram('Diagram transactions', 'flowchart')
        self.assertIsNotNone(result['code'])
        self.assertEqual(self.complete.call_count, 2)
        self.assertIn('Flowchart has no edges', self.complete.call_args.kwargs['messages'][1]['content'])
        self.complete.reset_mock(side_effect=True)
        self.complete.return_value = response('flowchart TD\n A[Begin]\n B[Commit]')
        self.assertEqual(diagrams.generate_diagram('Diagram transactions', 'flowchart')['answer'], diagrams.FAILURE)
        self.assertEqual(self.complete.call_count, 2)

    def test_no_passages_never_calls_generation_model(self):
        self.search.return_value = []
        result = diagrams.generate_diagram('Diagram transactions', 'flowchart', doc_ids=[])
        self.assertIsNone(result['code'])
        self.search.assert_called_once_with('Diagram transactions', k=8, source_doc_ids=[])
        self.complete.assert_not_called()

    def test_model_errors_are_friendly_and_usage_limits_are_not_retried(self):
        self.complete.side_effect = RuntimeError('Model unavailable')
        self.assertIsNone(diagrams.generate_diagram('Diagram transactions', 'flowchart')['code'])
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

    def test_dot_fallback_uses_existing_graphviz_renderer_format(self):
        self.diagram_language.return_value = 'dot'
        self.complete.return_value = response('digraph Notes { A [label="Begin"]; A -> B; B [label="Commit"]; }')
        result = diagrams.generate_diagram('Diagram transactions', 'flowchart')
        self.assertEqual(result['language'], 'dot')
        self.assertTrue(diagrams.validate_diagram(result['code'], 'dot'))
        self.assertIn('Return a digraph', self.complete.call_args.kwargs['messages'][0]['content'])

    def test_mindmap_and_sequence_generation_use_requested_syntax(self):
        for kind, code, expected in (
            ('mind map', 'mindmap\n root[Transactions]\n   a[Commit]', 'Start with mindmap'),
            ('sequence', 'sequenceDiagram\n A->>B: Commit', 'Start with sequenceDiagram'),
        ):
            with self.subTest(kind=kind):
                self.complete.return_value = response(code)
                result = diagrams.generate_diagram('Transaction notes', kind)
                self.assertIsNotNone(result['code'])
                self.assertIn(expected, self.complete.call_args.kwargs['messages'][0]['content'])


class DiagramIntegrationTests(unittest.TestCase):
    def test_chat_stores_fenced_diagram_and_sources_for_history(self):
        code = diagrams.sanitize_diagram(FLOWCHART)
        result = {'answer': f'```mermaid\n{code}\n```\n\nFrom your notes. [Transactions, p.2]', 'sources': [SOURCE]}
        with tempfile.TemporaryDirectory() as root, workspaces.storage_context(Path(root)):
            db.init_db()
            chat_id = chats.create_chat()
            with patch.object(qa, 'generate_diagram', return_value=result) as generate, \
                    patch.object(qa, '_answer_question') as text_answer:
                self.assertEqual(qa.answer_question('Show a flow chart', chat_id=chat_id), result)
                generate.assert_called_once_with('Show a flow chart', 'flowchart')
                text_answer.assert_not_called()
            history = chats.list_messages(chat_id)
            self.assertEqual([item['role'] for item in history], ['user', 'assistant'])
            self.assertEqual(history[-1]['sources'], [SOURCE])
            self.assertEqual(history[-1]['kind'], 'diagram')
            self.assertEqual(diagrams.parse_diagram_message(history[-1]['content'])['code'], code)

    def test_diagram_tool_passes_parameters_and_saves_result(self):
        with tempfile.TemporaryDirectory() as root, workspaces.storage_context(Path(root)):
            chat_id = chats.create_chat()
            state = {'selected_chat_id': chat_id}
            parameters = {'question': 'Transactions', 'kind': 'mind map', 'doc_ids': [3]}
            code = diagrams.sanitize_diagram(FLOWCHART)
            result = {'answer': f'```mermaid\n{code}\n```', 'code': code, 'sources': [SOURCE]}
            with patch.object(workflows, 'generate_diagram', return_value=result) as generate:
                workflows.run_tool(state, 'diagram', parameters)
                generate.assert_called_once_with(**parameters)
            message = chats.list_messages(chat_id)[0]
            self.assertEqual(message['kind'], 'diagram')
            self.assertEqual(message['content'], result['answer'])
            self.assertEqual(state['view'], 'chat')

    def test_enlarged_diagram_state_validates_content_and_clears_on_chat_change(self):
        state = {'selected_chat_id': 7}
        content = f'```mermaid\n{FLOWCHART}\n```\n\nFrom notes.'
        workflows.open_diagram_view(state, content, 'chat_7_message_2')
        self.assertEqual(workflows.active_diagram_view(state)['message_key'], 'chat_7_message_2')
        workflows.close_diagram_view(state)
        self.assertIsNone(workflows.active_diagram_view(state))
        with self.assertRaises(ValueError):
            workflows.open_diagram_view(state, 'Not a diagram', 'invalid')
        workflows.open_diagram_view(state, content, 'chat_7_message_2')
        state['selected_chat_id'] = 8
        self.assertIsNone(workflows.active_diagram_view(state))
        self.assertNotIn('diagram_view', state)

    def test_native_mermaid_capability_is_detected(self):
        import streamlit as st
        available = callable(getattr(st, 'mermaid_chart', None)) or callable(getattr(st, 'mermaid', None))
        self.assertEqual(diagrams.diagram_language(), 'mermaid' if available else 'dot')
        with patch.object(st, 'mermaid_chart', None, create=True), patch.object(st, 'mermaid', None, create=True):
            self.assertEqual(diagrams.diagram_language(), 'dot')

    def test_history_rendering_uses_native_charts_and_unique_view_code_keys(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text(encoding='utf-8'))
        nodes = [item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name in {'show_answer', 'show_diagram_chart'}]
        from unittest.mock import MagicMock
        ui = MagicMock()
        ui.session_state = {"theme_preset": "raven", "accent_name": "indigo"}
        namespace = {'st': ui, 'parse_diagram_message': diagrams.parse_diagram_message,
                     'run_callback': Mock(), 'flow': workflows, 'presentation_code': diagrams.presentation_code}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<diagram-ui>', 'exec'), namespace)
        for message_key in ('chat_1_message_2', 'chat_1_message_4'):
            namespace['show_answer'](f'```mermaid\n{FLOWCHART}\n```\n\nFrom notes.', message_key)
        self.assertEqual(ui.mermaid_chart.call_count, 2)
        self.assertTrue(all(call.kwargs['width'] == 'content' for call in ui.mermaid_chart.call_args_list))
        self.assertEqual(ui.button.call_count, 2)
        self.assertTrue(all(call.args[0] == 'Enlarge' for call in ui.button.call_args_list))
        self.assertTrue(all(call.kwargs['args'][0] is workflows.open_diagram_view for call in ui.button.call_args_list))
        self.assertEqual(len(set(call.kwargs['key'] for call in ui.expander.call_args_list)), 2)
        self.assertTrue(all(call.args[0] == 'View code' for call in ui.expander.call_args_list))
        ui.code.assert_called_with(diagrams.sanitize_diagram(FLOWCHART), language='mermaid')
        namespace['show_answer']('```dot\ndigraph Notes { A -> B; }\n```\n\nFrom notes.', 'tool_diagram')
        ui.graphviz_chart.assert_called_once()
        namespace['show_answer']('Ordinary answer', 'chat_1_message_6')
        ui.markdown.assert_called_once_with('Ordinary answer')


if __name__ == '__main__':
    unittest.main()
