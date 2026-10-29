import ast
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

with patch("dotenv.load_dotenv"):
    from src import branding, config, diagrams, themes


def wcag_contrast(first, second):
    def luminance(color):
        values = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        values = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
        return sum(v * weight for v, weight in zip(values, (0.2126, 0.7152, 0.0722)))
    brighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (brighter + 0.05) / (darker + 0.05)


class ThemesTests(unittest.TestCase):
    def test_exactly_four_presets_have_accessible_text_and_muted_contrast(self):
        self.assertEqual(set(themes.PRESETS), {"auto", "light", "dark", "eye_comfort"})
        for name, colors in themes.PRESETS.items():
            with self.subTest(theme=name):
                self.assertEqual(set(colors), {"bg", "surface", "surface2", "border", "text", "muted"})
                self.assertGreaterEqual(wcag_contrast(colors["text"], colors["bg"]), 4.5)
                self.assertGreaterEqual(wcag_contrast(colors["text"], colors["surface"]), 4.5)
                self.assertGreaterEqual(wcag_contrast(colors["muted"], colors["bg"]), 3)
        self.assertEqual(wcag_contrast("#000000", "#FFFFFF"), 21)
        self.assertEqual(themes.contrast("#000000", "#FFFFFF"), 21)

    def test_all_accents_choose_higher_contrast_text_and_readable_light_variant(self):
        self.assertEqual(set(themes.ACCENTS), {"blue", "violet", "green", "amber", "red", "slate"})
        for name, color in themes.ACCENTS.items():
            with self.subTest(accent=name):
                text = themes.accent_text(name)
                self.assertIn(text, {"#000000", "#FFFFFF"})
                self.assertGreaterEqual(wcag_contrast(color, text), 4.5)
                self.assertGreaterEqual(wcag_contrast(color, text), max(wcag_contrast(color, other) for other in ("#000000", "#FFFFFF")))
                variant = themes.light_accent_text(name)
                self.assertGreaterEqual(wcag_contrast(variant, themes.PRESETS["light"]["bg"]), 4.5)
                self.assertGreaterEqual(wcag_contrast(variant, themes.PRESETS["light"]["surface"]), 4.5)
                self.assertNotEqual(variant, color)

    def test_invalid_query_values_are_ignored_and_cannot_enter_css(self):
        for invalid in ("unknown", "#FFFFFF", "</style><script>alert(1)</script>", ["raven"], None):
            with self.subTest(value=invalid):
                query = {"w": "a" * 32, "t": invalid, "a": invalid}
                state = {}
                self.assertEqual(themes.initialize_appearance(state, query), ("auto", "blue"))
                self.assertEqual(query, {"w": "a" * 32, "t": "auto", "a": "blue"})
                css = themes.styles(invalid, invalid)
                self.assertEqual(css, themes.styles())
                self.assertNotIn("<script>", css)
        state = {"theme_preset": "missing", "accent_name": "unknown"}
        self.assertEqual(themes.initialize_appearance(state, {"t": "bad", "a": "bad"}), ("auto", "blue"))

    def test_valid_appearance_round_trip_keeps_workspace_and_other_query_values(self):
        state = {}
        query = {"w": "b" * 32, "t": "light", "a": "green", "other": "preserved"}
        self.assertEqual(themes.initialize_appearance(state, query), ("light", "green"))
        state.update(theme_preset="eye_comfort", accent_name="amber")
        themes.change_appearance(state, query)
        self.assertEqual(query, {"w": "b" * 32, "t": "eye_comfort", "a": "amber", "other": "preserved"})
        self.assertEqual(themes.initialize_appearance({}, query), ("eye_comfort", "amber"))
        state.update(theme_preset="invalid", accent_name="invalid")
        themes.change_appearance(state, query)
        self.assertEqual((query["t"], query["a"], query["w"]), ("auto", "blue", "b" * 32))
        for old_theme, old_accent, expected in (("raven", "indigo", ("dark", "blue")),
                                                 ("midnight", "teal", ("dark", "green")),
                                                 ("light", "rose", ("light", "red"))):
            with self.subTest(theme=old_theme, accent=old_accent):
                self.assertEqual(themes.initialize_appearance({}, {"t": old_theme, "a": old_accent}), expected)

    def test_css_covers_widgets_and_eye_comfort_contains_no_white_text_color(self):
        css = themes.styles("eye_comfort", "amber")
        self.assertIn("--cv-text:#E3DCCB", css)
        self.assertNotIn("#FFFFFF", css)
        for token in ("stSidebar", "stChatInput", "stChatMessage", "stExpander", "stPopoverBody", "dialog",
                      "textarea", "input", "listbox", "stCode", "table", "tooltip", "scrollbar", "sources_panel", "diagram_canvas"):
            self.assertIn(token, css)
        self.assertEqual(css.count("<style>"), 1)
        base = (Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml").read_text(encoding="utf-8")
        import tomllib
        self.assertEqual(tomllib.loads(base)["theme"], {"primaryColor": "#4F7CFF"})
        self.assertIn("@media (prefers-color-scheme: dark)", themes.styles())

    def test_diagram_palette_is_fixed_and_does_not_mutate_saved_spec(self):
        source = {"type": "flowchart", "title": "Transactions", "nodes": [
            {"id": "A", "label": "Begin", "kind": "start"}, {"id": "B", "label": "Commit", "kind": "end"}],
            "edges": [{"from": "A", "to": "B"}]}
        original = json.dumps(source)
        for name in themes.PRESETS:
            graph = MagicMock()
            module = SimpleNamespace(Digraph=Mock(return_value=graph), escape=lambda label: label)
            with patch.dict("sys.modules", {"graphviz": module}):
                diagrams._build_graph(source)
            self.assertEqual(graph.attr.call_args_list[0].kwargs["bgcolor"], "#FFFFFF")
            self.assertEqual(graph.attr.call_args_list[0].kwargs["dpi"], "200")
            self.assertTrue(all(call.kwargs["fillcolor"] == "#DCFCE7" for call in graph.node.call_args_list))
            self.assertEqual(json.dumps(source), original)


class BrandingTests(unittest.TestCase):
    def test_missing_and_unreadable_logo_return_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIsNone(branding.load_logo("dark", temporary))
            self.assertIsNone(branding.load_logo("light", temporary))
            path = Path(temporary) / "raven.png"
            path.write_bytes(b"invalid image data")
            self.assertIsNone(branding.load_logo("dark", temporary))
        self.assertTrue(branding.DEFAULT_ICON)

    def test_light_logo_is_selected_only_for_light_and_falls_back_to_raven(self):
        normal, light = object(), object()
        def read(path, version):
            return light if Path(path).name == "raven_light.png" else normal
        with patch.object(Path, "stat", return_value=SimpleNamespace(st_mtime_ns=1, st_size=10)), \
                patch.object(branding, "_load_image", side_effect=read) as load:
            self.assertIs(branding.load_logo("light"), light)
            for theme in ("auto", "dark", "eye_comfort"):
                self.assertIs(branding.load_logo(theme), normal)
            self.assertEqual(sum(Path(call.args[0]).name == "raven_light.png" for call in load.call_args_list), 1)
        with patch.object(Path, "stat", return_value=SimpleNamespace(st_mtime_ns=2, st_size=10)), \
                patch.object(branding, "_load_image", side_effect=[None, normal]):
            self.assertIs(branding.load_logo("light"), normal)

    def test_logo_loader_is_cached_and_detaches_from_closed_pillow_file(self):
        branding._load_image.clear()
        image = MagicMock()
        sentinel = object()
        image.__enter__.return_value = image
        image.copy.return_value = sentinel
        with patch.object(branding.Image, "open", return_value=image) as opened:
            self.assertIs(branding._load_image("fixture.png", (123, 5)), sentinel)
            self.assertIs(branding._load_image("fixture.png", (123, 5)), sentinel)
            opened.assert_called_once()
            image.load.assert_called_once()
            image.__exit__.assert_called_once()
        branding._load_image.clear()

    def test_header_and_page_title_use_app_name_with_logo_and_fallback(self):
        self.assertEqual(config.APP_NAME, "Corvium")
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        header = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "show_sidebar_header")
        ui = MagicMock()
        namespace = {"st": ui, "APP_NAME": "Test product"}
        exec(compile(ast.Module(body=[header], type_ignores=[]), "<brand-header>", "exec"), namespace)
        namespace["show_sidebar_header"](None)
        ui.image.assert_not_called()
        ui.markdown.assert_called_with("**Test product**")
        logo = object()
        namespace["show_sidebar_header"](logo)
        ui.image.assert_called_once_with(logo, width=40)
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        prefix = main.body[:next(i for i, node in enumerate(main.body) if isinstance(node, ast.Try))]
        ui.session_state, ui.query_params = {}, {"w": "c" * 32}
        namespace.update(themes=themes, load_logo=Mock(return_value=None), DEFAULT_ICON=branding.DEFAULT_ICON, bind_workspace=Mock())
        exec(compile(ast.Module(body=prefix, type_ignores=[]), "<page-brand>", "exec"), namespace)
        self.assertEqual(ui.set_page_config.call_args.kwargs["page_title"], "Test product")
        self.assertEqual(ui.set_page_config.call_args.kwargs["page_icon"], branding.DEFAULT_ICON)
        namespace["load_logo"].return_value = logo
        exec(compile(ast.Module(body=prefix, type_ignores=[]), "<page-brand>", "exec"), namespace)
        self.assertIs(ui.set_page_config.call_args.kwargs["page_icon"], logo)

    def test_empty_chat_uses_72px_logo_and_name_when_missing(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "show_empty_chat")
        ui = MagicMock()
        ui.session_state = {"theme_preset": "light"}
        ui.columns.return_value = [MagicMock(), MagicMock(), MagicMock()]
        loader = Mock(return_value=None)
        namespace = {"st": ui, "load_logo": loader, "APP_NAME": config.APP_NAME, "flow": Mock(), "run_callback": Mock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<empty-brand>", "exec"), namespace)
        namespace["show_empty_chat"]()
        ui.image.assert_not_called()
        self.assertIn("**Corvium**", [call.args[0] for call in ui.markdown.call_args_list])
        loader.assert_called_with("light")
        logo = object()
        loader.return_value = logo
        namespace["show_empty_chat"]()
        ui.image.assert_called_once_with(logo, width=72)


if __name__ == "__main__":
    unittest.main()
