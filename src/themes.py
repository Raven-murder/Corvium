PRESETS = {
    "raven": {"bg": "#0A0B10", "surface": "#12141C", "border": "#232738", "text": "#E8EAF2", "muted": "#9AA1B5"},
    "light": {"bg": "#F6F7FB", "surface": "#FFFFFF", "border": "#D9DCE8", "text": "#14161F", "muted": "#5B6178"},
    "midnight": {"bg": "#0A1020", "surface": "#121A2E", "border": "#22304F", "text": "#E6ECFA", "muted": "#98A6C6"},
    "eye_comfort": {"bg": "#1C1A17", "surface": "#25221E", "border": "#3A352E", "text": "#E3DCCB", "muted": "#A99F8C"},
}
ACCENTS = {"indigo": "#7C8CFF", "violet": "#A78BFA", "teal": "#2DD4BF", "cyan": "#38BDF8", "rose": "#F472B6", "amber": "#FBBF24"}
THEME_LABELS = {"raven": "Raven", "light": "Light", "midnight": "Midnight", "eye_comfort": "Eye comfort"}


def _rgb(color):
    return tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))


def luminance(color):
    channels = [value / 255 for value in _rgb(color)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def contrast(first, second):
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def accent_text(accent):
    color = ACCENTS.get(accent, ACCENTS["indigo"])
    return max(("#000000", "#FFFFFF"), key=lambda text: contrast(color, text))


def light_accent_text(accent):
    color = ACCENTS.get(accent, ACCENTS["indigo"])
    channels = _rgb(color)
    for percent in range(100, -1, -1):
        darkened = "#" + "".join(f"{round(value * percent / 100):02X}" for value in channels)
        if contrast(darkened, PRESETS["light"]["bg"]) >= 4.5:
            return darkened
    return "#000000"


def initialize_appearance(state, query):
    theme = query.get("t")
    accent = query.get("a")
    if not isinstance(theme, str) or theme not in PRESETS:
        theme = state.get("theme_preset", "raven")
    if not isinstance(accent, str) or accent not in ACCENTS:
        accent = state.get("accent_name", "indigo")
    theme = theme if isinstance(theme, str) and theme in PRESETS else "raven"
    accent = accent if isinstance(accent, str) and accent in ACCENTS else "indigo"
    state["theme_preset"], state["accent_name"] = theme, accent
    query["t"], query["a"] = theme, accent
    return theme, accent


def change_appearance(state, query):
    theme, accent = state.get("theme_preset"), state.get("accent_name")
    query["t"] = theme if isinstance(theme, str) and theme in PRESETS else "raven"
    query["a"] = accent if isinstance(accent, str) and accent in ACCENTS else "indigo"


def diagram_config(theme, accent):
    theme = theme if isinstance(theme, str) and theme in PRESETS else "raven"
    accent = accent if isinstance(accent, str) and accent in ACCENTS else "indigo"
    palette = PRESETS[theme]
    return {"theme": "neutral" if theme == "light" else "dark", "fontSize": 16,
            "themeVariables": {"fontSize": "16px", "primaryColor": palette["surface"],
                               "primaryTextColor": palette["text"], "primaryBorderColor": ACCENTS[accent],
                               "secondaryColor": palette["surface"], "tertiaryColor": palette["surface"],
                               "mainBkg": palette["surface"], "nodeBkg": palette["surface"], "nodeBorder": ACCENTS[accent],
                               "secondaryTextColor": palette["text"], "tertiaryTextColor": palette["text"],
                               "edgeLabelBackground": palette["surface"], "clusterBkg": palette["surface"],
                               "actorBkg": palette["surface"], "actorBorder": ACCENTS[accent],
                               "noteBkgColor": palette["surface"], "signalColor": palette["muted"],
                               "lineColor": palette["muted"], "textColor": palette["text"],
                               "actorTextColor": palette["text"], "signalTextColor": palette["text"],
                               "noteTextColor": palette["text"], "labelTextColor": palette["text"],
                               "background": palette["surface"]},
            "flowchart": {"useMaxWidth": False}, "sequence": {"useMaxWidth": False},
            "mindmap": {"useMaxWidth": False}}


def styles(theme="raven", accent="indigo"):
    theme = theme if isinstance(theme, str) and theme in PRESETS else "raven"
    accent = accent if isinstance(accent, str) and accent in ACCENTS else "indigo"
    palette = PRESETS[theme]
    properties = {"--cv-" + name: value for name, value in palette.items()}
    properties.update({"--cv-accent": ACCENTS[accent], "--cv-on-accent": accent_text(accent),
                       "--cv-accent-text": light_accent_text(accent) if theme == "light" else ACCENTS[accent],
                       "--cv-scheme": "light" if theme == "light" else "dark"})
    declarations = ";".join(f"{key}:{value}" for key, value in properties.items())
    return "<style>:root{" + declarations + ";}" + _CSS + "</style>"


_CSS = r"""
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
    background:var(--cv-bg); color:var(--cv-text); color-scheme:var(--cv-scheme);
}
.stApp {background:var(--cv-bg);}
[data-testid="stAppViewContainer"] {
    background:radial-gradient(ellipse at 55% 5%, color-mix(in srgb,var(--cv-accent) 6%,transparent), transparent 55%),var(--cv-bg);
}
[data-testid="stHeader"], [data-testid="stBottom"], [data-testid="stBottomBlockContainer"] {
    background:var(--cv-bg); color:var(--cv-text);
}
[data-testid="stSidebar"], [data-testid="stSidebarContent"] {background:var(--cv-surface); color:var(--cv-text);}
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
[data-testid="stMarkdownContainer"], [data-testid="stText"], [data-testid="stWidgetLabel"],
[data-testid="stChatMessageContent"], [role="dialog"], [role="dialog"] label {color:var(--cv-text);}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p,
[data-testid="stMarkdownContainer"] small {color:var(--cv-muted)!important;}
a {color:var(--cv-accent-text)!important;}
button, [data-testid="stDownloadButton"] button {
    color:var(--cv-text)!important; background:var(--cv-surface)!important; border-color:var(--cv-border)!important; border-radius:10px;
}
button p, button span, button [data-testid="stMarkdownContainer"], [role="dialog"] button p {color:inherit!important;}
button:hover {border-color:var(--cv-accent)!important; color:var(--cv-accent-text)!important;}
button:focus-visible {outline:2px solid var(--cv-accent); outline-offset:2px;}
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"],
[data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover {
    background:var(--cv-accent)!important; border-color:var(--cv-accent)!important; color:var(--cv-on-accent)!important;
}
button:disabled {color:var(--cv-muted)!important; opacity:.65;}
input, textarea, [data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="textarea"],
[data-baseweb="select"] > div, [data-testid="stNumberInputContainer"] {
    background:var(--cv-surface)!important; color:var(--cv-text)!important; border-color:var(--cv-border)!important;
    caret-color:var(--cv-accent);
}
.react-aria-ComboBox [role="group"], .react-aria-Select button,
[data-testid="stMultiSelect"] [role="group"], [data-testid="stTextInputRootElement"],
[data-testid="stTextAreaRootElement"] {
    background:var(--cv-surface)!important; color:var(--cv-text)!important; border-color:var(--cv-border)!important;
}
.react-aria-ComboBox [role="group"]:focus-within, [data-testid="stTextInputRootElement"]:focus-within,
[data-testid="stTextAreaRootElement"]:focus-within, [data-baseweb="input"]:focus-within {
    border-color:var(--cv-accent)!important;
}
.st-key-selected_chat_id .react-aria-ComboBox [role="group"] {border-color:var(--cv-accent)!important;}
input::placeholder, textarea::placeholder {color:var(--cv-muted)!important; opacity:1;}
[data-baseweb="select"] span, [data-baseweb="select"] input {color:var(--cv-text)!important;}
[data-baseweb="select"] svg, [data-baseweb="input"] svg {fill:var(--cv-muted);}
[data-baseweb="tag"] {background:var(--cv-accent)!important; color:var(--cv-on-accent)!important;}
[data-baseweb="tag"] span {color:var(--cv-on-accent)!important;}
[data-testid="stMultiSelect"] [data-tag], [data-testid="stMultiSelect"] [data-tag] button {
    background:var(--cv-accent)!important; color:var(--cv-on-accent)!important; border-color:var(--cv-accent)!important;
}
[data-testid="stMultiSelect"] [data-tag] span {color:var(--cv-on-accent)!important;}
[data-baseweb="popover"], [data-baseweb="menu"], [role="listbox"], [role="option"] {
    background:var(--cv-surface)!important; color:var(--cv-text)!important; border-color:var(--cv-border)!important;
}
[role="option"]:hover, [role="option"][aria-selected="true"] {background:color-mix(in srgb,var(--cv-accent) 15%,var(--cv-surface))!important;}
[data-testid="stExpander"] {border-color:var(--cv-border); border-radius:12px; background:transparent; color:var(--cv-text);}
[data-testid="stExpander"] summary, [data-testid="stExpander"] summary p {color:var(--cv-text);}
[data-testid="stPopoverBody"], [role="dialog"], [data-testid="stDialog"] > div {
    background:var(--cv-surface)!important; color:var(--cv-text)!important; border-color:var(--cv-border)!important; border-radius:12px;
}
[role="tablist"] {border-color:var(--cv-border);}
[role="tab"] {background:transparent; color:var(--cv-muted)!important;}
[role="tab"][aria-selected="true"] {color:var(--cv-accent-text)!important; border-color:var(--cv-accent);}
[data-baseweb="tab-highlight"] {background:var(--cv-accent);}
input[type="radio"], input[type="checkbox"] {accent-color:var(--cv-accent);}
[data-testid="stRadio"] label, [data-testid="stCheckbox"] label {color:var(--cv-text);}
[data-testid="stRadioOption"] > div > div:first-child {
    background:var(--cv-surface)!important; border-color:var(--cv-muted)!important;
}
[data-testid="stRadioOption"][data-selected="true"] > div > div:first-child {
    background:var(--cv-accent)!important; border-color:var(--cv-accent)!important;
}
[data-testid="stRadioOption"] > div > div:first-child > div {background:var(--cv-on-accent)!important;}
[data-testid="stCode"], pre, code {background:var(--cv-surface)!important; color:var(--cv-text)!important; border-color:var(--cv-border);}
[data-testid="stCode"] span {color:var(--cv-text)!important;}
[data-testid="stMarkdownContainer"] table {color:var(--cv-text); border-color:var(--cv-border);}
[data-testid="stMarkdownContainer"] th, [data-testid="stMarkdownContainer"] td {border-color:var(--cv-border);}
[data-testid="stMarkdownContainer"] th {background:var(--cv-surface); color:var(--cv-text);}
[data-testid="stMarkdownContainer"] tr:nth-child(even) {background:color-mix(in srgb,var(--cv-surface) 65%,var(--cv-bg));}
[role="tooltip"], [data-testid="stTooltipContent"], [data-baseweb="tooltip"] {
    background:var(--cv-surface)!important; color:var(--cv-text)!important; border:1px solid var(--cv-border);
}
[role="tooltip"] p {color:var(--cv-text);}
* {scrollbar-color:var(--cv-border) var(--cv-bg); scrollbar-width:thin;}
::-webkit-scrollbar {width:10px; height:10px;}
::-webkit-scrollbar-track {background:var(--cv-bg);}
::-webkit-scrollbar-thumb {background:var(--cv-border); border-radius:8px; border:2px solid var(--cv-bg);}
.st-key-selected_chat_id [data-baseweb="select"] > div {border-color:var(--cv-accent)!important; border-radius:10px;}
[class*="st-key-chat_active_"] {border-left:2px solid var(--cv-accent); border-radius:8px; padding-left:.4rem; background:color-mix(in srgb,var(--cv-accent) 8%,transparent);}
[class*="st-key-pin_on_"] [data-testid="stIconMaterial"] {font-variation-settings:'FILL' 1; color:var(--cv-accent-text);}
[class*="st-key-pin_off_"] [data-testid="stIconMaterial"] {font-variation-settings:'FILL' 0;}
.stMain {overflow-y:auto; scroll-padding-top:6rem; scroll-padding-bottom:9rem;}
.stMain > #Main {width:100%; flex-shrink:0;}
[data-testid="stMainBlockContainer"] {max-width:1008px; margin-inline:auto; padding:6rem 1.5rem 9rem; flex-shrink:0;}
[data-testid="stBottomBlockContainer"] {max-width:1008px; margin-inline:auto;}
[data-testid="stChatMessage"] {width:100%; padding:0; background:transparent; gap:0; border-radius:12px; color:var(--cv-text);}
[data-testid^="stChatMessageAvatar"] {display:none;}
[data-testid="stChatMessageContent"] {width:100%; min-width:0; margin:0; flex:1 1 100%;}
[data-testid="stChatMessage"]:has([aria-label="Chat message from user"]) {justify-content:flex-end;}
[data-testid="stChatMessageContent"][aria-label="Chat message from user"] {
    width:fit-content; max-width:80%; flex:0 1 auto; margin-left:auto; padding:.85rem 1rem;
    background:color-mix(in srgb,var(--cv-accent) 12%,var(--cv-bg));
    border:1px solid color-mix(in srgb,var(--cv-accent) 18%,var(--cv-border)); border-radius:16px 16px 4px 16px;
}
[data-testid="stChatMessageContent"] [data-testid="stMarkdownContainer"]:has(table) {overflow-x:auto;}
[data-testid="stChatMessageContent"] table {width:100%; table-layout:auto;}
[class*="st-key-diagram_canvas_"] {width:100%; min-width:0; background:var(--cv-surface); border:1px solid var(--cv-border); border-radius:12px;}
[class*="st-key-diagram_canvas_inline_"] {height:420px!important; max-height:420px; overflow:auto!important;}
[class*="st-key-diagram_canvas_large_"] {min-height:480px; max-height:72dvh; overflow:auto!important;}
[class*="st-key-diagram_canvas_"] [data-testid="stMermaidChart"] {width:max-content!important; min-width:100%; padding:.5rem; align-items:flex-start!important;}
[class*="st-key-diagram_canvas_"] img, [class*="st-key-diagram_canvas_"] svg, [class*="st-key-diagram_canvas_"] canvas {
    width:auto!important; max-width:none!important; height:auto!important; max-height:none!important; flex-shrink:0;
}
.stMain [class*="st-key-sources_panel_"] {
    position:fixed; top:5rem; right:1rem; width:380px; height:calc(100dvh - 13rem);
    min-width:280px; min-height:220px; max-width:calc(100vw - 2rem); max-height:calc(100dvh - 7rem);
    overflow:auto; resize:both; z-index:999993; padding:.8rem; border:1px solid var(--cv-border);
    border-radius:14px; background:var(--cv-surface); color:var(--cv-text); box-shadow:0 12px 36px color-mix(in srgb,var(--cv-bg) 55%,transparent);
}
.stMain .st-key-sources_panel_left {left:1rem; right:auto;}
.stMain .st-key-sources_panel_bottom {top:auto; right:auto; bottom:8rem; left:50%; transform:translateX(-50%); width:960px; height:300px;}
.st-key-sources_header {position:sticky; top:-.8rem; z-index:1; background:var(--cv-surface); padding:.35rem 0;}
.st-key-sources_header button {min-width:1.8rem; padding:.15rem;}
[data-testid="stChatInput"] {
    max-width:760px; margin-inline:auto; border:1px solid var(--cv-accent); border-radius:18px;
    background:var(--cv-surface); color:var(--cv-text);
    box-shadow:0 0 0 1px color-mix(in srgb,var(--cv-accent) 12%,transparent),0 6px 24px color-mix(in srgb,var(--cv-accent) 14%,transparent);
}
[data-testid="stChatInput"]:focus-within {box-shadow:0 0 0 2px color-mix(in srgb,var(--cv-accent) 30%,transparent),0 6px 26px color-mix(in srgb,var(--cv-accent) 20%,transparent);}
[data-testid="stChatInput"] > div {background:var(--cv-surface)!important; border-radius:inherit;}
[data-testid="stChatInput"] textarea {background:transparent!important; color:var(--cv-text)!important;}
[data-testid="stChatInputSubmitButton"] {color:var(--cv-accent-text)!important;}
[data-testid="stChatMessage"] [data-testid="stPopover"] > button {min-height:1.75rem; padding:.1rem .25rem; justify-content:flex-start;}
.st-key-empty_chat {margin-top:clamp(3rem,16vh,9rem);}
.st-key-empty_mascot {display:flex; align-items:center; margin-bottom:1rem;}
.st-key-empty_mascot [data-testid="stImage"] {margin-inline:auto;}
.st-key-empty_mascot [data-testid="stMarkdownContainer"] {text-align:center; width:100%;}
.chat-greeting {text-align:center; font-size:clamp(1.65rem,3vw,2.1rem); font-weight:600; color:var(--cv-text);}
.chat-subtitle {text-align:center; color:var(--cv-muted); margin:.6rem 0 1.5rem; font-size:.95rem;}
.st-key-empty_chat button {border-radius:20px; border-color:var(--cv-border); background:var(--cv-surface); color:var(--cv-text);}
.st-key-empty_chat button:hover {border-color:var(--cv-accent);}
.st-key-brand_header {gap:.5rem; padding-block:0; min-height:28px; margin-bottom:.2rem;}
.st-key-brand_header [data-testid="stMarkdownContainer"] p {margin:0;}
.st-key-brand_header [data-testid="stImage"] {flex:0 0 28px; margin:0;}
.st-key-tips pre {font-size:.75rem; line-height:1.4;}
[data-testid="stPopoverBody"] {border:0; padding:.5rem;}
[data-testid="stPopoverBody"] button {border:0; padding:.15rem .35rem;}
[data-testid="stPopoverBody"] [data-baseweb="input"], [data-testid="stPopoverBody"] [data-baseweb="select"] > div,
.st-key-manage_chats [data-baseweb="input"] {border:0; box-shadow:none;}
[data-testid="stPopoverBody"] [data-testid="stFileUploaderDropzone"] {border:0; padding:.25rem; background:transparent;}
.st-key-library_controls button {border:0; padding:.15rem .35rem;}
.st-key-sidebar_footer {margin-top:auto; padding-top:1.5rem;}
[data-testid="stSidebarUserContent"] > [data-testid="stVerticalBlock"] {min-height:calc(100dvh - 5rem);}
"""
