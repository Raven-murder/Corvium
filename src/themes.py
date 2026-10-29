PRESETS = {
    "auto": {"bg": "#EEF0F6", "surface": "#FFFFFF", "surface2": "#FFFFFF", "border": "#C9CEE0", "text": "#14161F", "muted": "#5B6178"},
    "light": {"bg": "#EEF0F6", "surface": "#FFFFFF", "surface2": "#FFFFFF", "border": "#C9CEE0", "text": "#14161F", "muted": "#5B6178"},
    "dark": {"bg": "#0A0B10", "surface": "#12141C", "surface2": "#1B1F2E", "border": "#2A3046", "text": "#E8EAF2", "muted": "#9AA1B5"},
    "eye_comfort": {"bg": "#1C1A17", "surface": "#25221E", "surface2": "#2F2B25", "border": "#433D33", "text": "#E3DCCB", "muted": "#A99F8C"},
}
ACCENTS = {"blue": "#4F7CFF", "violet": "#8B5CF6", "green": "#22C55E", "amber": "#F5A524", "red": "#EF4444", "slate": "#94A3B8"}
THEME_LABELS = {"auto": "Auto", "light": "Light", "dark": "Dark", "eye_comfort": "Eye comfort"}
_THEME_ALIASES = {"raven": "dark", "midnight": "dark"}
_ACCENT_ALIASES = {"indigo": "blue", "cyan": "blue", "teal": "green", "rose": "red"}


def _theme(value):
    return _THEME_ALIASES.get(value, value) if isinstance(value, str) else None


def _accent(value):
    return _ACCENT_ALIASES.get(value, value) if isinstance(value, str) else None


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
    color = ACCENTS.get(_accent(accent), ACCENTS["blue"])
    return max(("#000000", "#FFFFFF"), key=lambda text: contrast(color, text))


def light_accent_text(accent):
    channels = _rgb(ACCENTS.get(_accent(accent), ACCENTS["blue"]))
    for percent in range(99, -1, -1):
        darkened = "#" + "".join(f"{round(value * percent / 100):02X}" for value in channels)
        if min(contrast(darkened, PRESETS["light"][surface]) for surface in ("bg", "surface")) >= 4.5:
            return darkened
    return "#000000"


def initialize_appearance(state, query):
    theme = _theme(query.get("t", state.get("theme_preset", "auto")))
    accent = _accent(query.get("a", state.get("accent_name", "blue")))
    theme = theme if theme in PRESETS else "auto"
    accent = accent if accent in ACCENTS else "blue"
    state["theme_preset"], state["accent_name"] = theme, accent
    query["t"], query["a"] = theme, accent
    return theme, accent


def change_appearance(state, query):
    theme, accent = _theme(state.get("theme_preset")), _accent(state.get("accent_name"))
    state["theme_preset"] = query["t"] = theme if theme in PRESETS else "auto"
    state["accent_name"] = query["a"] = accent if accent in ACCENTS else "blue"


def _properties(theme, accent):
    palette = PRESETS[theme]
    color = ACCENTS[accent]
    red, green, blue = _rgb(color)
    light = theme in {"auto", "light"}
    properties = {"--cv-" + name: value for name, value in palette.items()}
    properties.update({"--cv-accent": color, "--cv-accent-outline": f"rgba({red},{green},{blue},0.55)",
                       "--cv-on-accent": accent_text(accent),
                       "--cv-accent-text": light_accent_text(accent) if light else color,
                       "--cv-scheme": "light" if light else "dark",
                       "--cv-popup-gradient": f"linear-gradient(135deg,rgba({red},{green},{blue},0.06),rgba({red},{green},{blue},0) 65%)",
                       "--cv-popup-shadow": "0 16px 48px rgba(20,22,31,0.20)" if light else "0 12px 36px rgba(0,0,0,0.30)"})
    return ";".join(f"{key}:{value}" for key, value in properties.items())


def styles(theme="auto", accent="blue"):
    theme, accent = _theme(theme), _accent(accent)
    theme = theme if theme in PRESETS else "auto"
    accent = accent if accent in ACCENTS else "blue"
    declarations = ":root{" + _properties(theme, accent) + ";}"
    if theme == "auto":
        declarations += "@media (prefers-color-scheme: dark){:root{" + _properties("dark", accent) + ";}}"
    return "<style>" + declarations + _CSS + "</style>"


_CSS = r"""
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
    background:var(--cv-bg); color:var(--cv-text); color-scheme:var(--cv-scheme);
}
.stApp {background:var(--cv-bg);}
[data-testid="stAppViewContainer"] {
    background:radial-gradient(ellipse at 55% 5%, color-mix(in srgb,var(--cv-accent) 6%,transparent), transparent 55%),var(--cv-bg);
}
[data-testid="stHeader"], [data-testid="stHeader"] div,
[data-testid="stBottom"], [data-testid="stBottomBlockContainer"],
[data-testid="stBottom"] div:not([data-testid="stChatInput"]):not([data-testid="stChatInput"] *),
[data-testid="stBottomBlockContainer"] div:not([data-testid="stChatInput"]):not([data-testid="stChatInput"] *) {
    background:var(--cv-bg)!important; background-image:none!important; color:var(--cv-text);
}
[data-testid="stHeader"]::before, [data-testid="stHeader"]::after,
[data-testid="stBottom"]::before, [data-testid="stBottom"]::after {background:var(--cv-bg)!important; background-image:none!important;}
[data-testid="stSidebar"], [data-testid="stSidebarContent"] {background:var(--cv-surface); color:var(--cv-text);}
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
[data-testid="stMarkdownContainer"], [data-testid="stText"], [data-testid="stWidgetLabel"],
[data-testid="stChatMessageContent"], [role="dialog"], [role="dialog"] label {color:var(--cv-text);}
[data-testid="stText"], [data-testid="stText"] span {color:var(--cv-text)!important;}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] :is(p,span,li),
[data-testid="stMarkdownContainer"] small {color:var(--cv-muted)!important;}
[data-testid="stCaptionContainer"] {opacity:1!important;}
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
    background:var(--cv-surface2)!important; color:var(--cv-text)!important; border-color:var(--cv-border)!important;
}
[role="option"]:hover, [role="option"][aria-selected="true"] {background:color-mix(in srgb,var(--cv-accent) 15%,var(--cv-surface))!important;}
[data-testid="stExpander"] {border-color:var(--cv-border); border-radius:12px; background:transparent; color:var(--cv-text);}
[data-testid="stExpander"] summary,
[data-testid="stExpander"] details[open] > summary,
[data-testid="stExpander"] summary:is(:hover,:focus,:focus-visible,:active) {
    background:var(--cv-surface)!important; color:var(--cv-text)!important; opacity:1!important;
}
[data-testid="stExpander"] summary :is(p,span,[data-testid="stMarkdownContainer"]) {color:inherit!important;}
[data-testid="stSidebar"] :is(.st-key-manage_chats,.st-key-pinned_messages,.st-key-tools,.st-key-library,
    .st-key-document_search,.st-key-bookmarks,.st-key-memory,.st-key-tips,.st-key-appearance)
    > [data-testid="stExpander"] > details > summary > span {justify-content:center;}
[data-testid="stSidebar"] :is(.st-key-manage_chats,.st-key-pinned_messages,.st-key-tools,.st-key-library,
    .st-key-document_search,.st-key-bookmarks,.st-key-memory,.st-key-tips,.st-key-appearance)
    > [data-testid="stExpander"] > details > summary > span > div {width:auto; flex-grow:0;}

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
[class*="st-key-message_group_user_"] [data-testid="stChatMessage"] {
    width:fit-content!important; min-width:fit-content!important; max-width:75%; flex:0 1 auto; margin-left:auto; padding:.85rem 1rem;
    background:color-mix(in srgb,var(--cv-accent) 12%,var(--cv-bg));
    border:1px solid color-mix(in srgb,var(--cv-accent) 18%,var(--cv-border)); border-radius:16px 16px 4px 16px;
}
[class*="st-key-message_group_user_"] [data-testid="stChatMessageContent"] {
    width:auto!important; min-width:0; max-width:100%; flex:0 1 auto; margin:0; overflow-wrap:normal; word-break:normal; white-space:normal;
}
[class*="st-key-message_actions_"] {display:flex!important; flex-direction:row!important; flex-wrap:nowrap!important; gap:.4rem!important;}
[class*="st-key-message_actions_"] > [data-testid="stElementContainer"] {width:fit-content!important; flex:0 0 auto!important;}
[class*="st-key-message_actions_"] button {width:fit-content!important; min-width:1.8rem; padding:.1rem .3rem;}
[class*="st-key-message_group_user_"] [class*="st-key-message_actions_"] {justify-content:flex-end!important;}
[data-testid="stChatMessageContent"] [data-testid="stMarkdownContainer"]:has(table) {overflow-x:auto;}
[data-testid="stChatMessageContent"] table {width:100%; table-layout:auto;}
[class*="st-key-diagram_canvas_"] {
    width:100%; min-width:0; background:rgb(255 255 255)!important; color:rgb(17 24 39)!important;
    border:1px solid rgb(201 206 224); border-radius:12px; padding:.75rem; overflow:auto!important;
}
[class*="st-key-diagram_canvas_inline_"] {max-height:480px!important; height:auto;}
[class*="st-key-diagram_canvas_large_"] {max-height:72dvh; min-height:240px;}
[class*="st-key-diagram_canvas_inline_"] [data-testid="stImage"],
[class*="st-key-diagram_canvas_inline_"] img {width:100%!important; max-width:100%!important; height:auto!important;}
[class*="st-key-diagram_canvas_large_"] [data-testid="stImage"],
[class*="st-key-diagram_canvas_large_"] img {width:max-content!important; max-width:none!important; height:auto!important;}
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
.st-key-brand_header {gap:.5rem; padding-block:0; min-height:40px; margin-bottom:.2rem;}
.st-key-brand_header [data-testid="stMarkdownContainer"] p {margin:0; font-size:1.6rem; font-weight:700; letter-spacing:-.035em; line-height:1.2;}
.st-key-brand_header [data-testid="stImage"] {flex:0 0 40px; margin:0;}
.st-key-tips pre {font-size:.75rem; line-height:1.4;}
[data-testid="stPopoverBody"] {border:0; padding:.5rem;}
[data-testid="stPopoverBody"] button {border:0; padding:.15rem .35rem;}
[data-testid="stPopoverBody"] [data-baseweb="input"], [data-testid="stPopoverBody"] [data-baseweb="select"] > div,
.st-key-manage_chats [data-baseweb="input"] {border:0; box-shadow:none;}
[data-testid="stPopoverBody"] [data-testid="stFileUploaderDropzone"] {border:0; padding:.25rem; background:transparent;}
.st-key-library_controls button {border:0; padding:.15rem .35rem;}
.st-key-sidebar_footer {margin-top:auto; padding-top:1.5rem;}
[data-testid="stSidebarUserContent"] > [data-testid="stVerticalBlock"] {min-height:calc(100dvh - 5rem);}
[data-testid="stSidebar"] :is(button, [data-testid="stExpander"], summary, [data-testid="stExpanderDetails"],
    [data-testid="stTextInputRootElement"], [data-testid="stTextAreaRootElement"], [data-testid="stNumberInputContainer"],
    .react-aria-ComboBox > [role="group"], [data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div),
[data-testid="stPopoverBody"], [data-testid="stPopoverBody"] :is(button, [data-testid="stTextInputRootElement"],
    [data-testid="stTextAreaRootElement"], .react-aria-ComboBox > [role="group"], [data-baseweb="input"], [data-baseweb="select"] > div) {
    outline:1px solid var(--cv-accent-outline)!important; outline-offset:-1px; border-color:var(--cv-accent-outline)!important;
}
[data-testid="stSidebar"] :is(button, [data-testid="stExpander"], summary, [data-testid="stExpanderDetails"],
    [data-testid="stTextInputRootElement"], [data-testid="stTextAreaRootElement"], [data-testid="stNumberInputContainer"],
    .react-aria-ComboBox > [role="group"], [data-baseweb="input"], [data-baseweb="select"] > div):is(:hover,:focus,:focus-within),
[data-testid="stPopoverBody"]:is(:hover,:focus-within),
[data-testid="stPopoverBody"] :is(button, [data-testid="stTextInputRootElement"], [data-testid="stTextAreaRootElement"],
    .react-aria-ComboBox > [role="group"], [data-baseweb="input"], [data-baseweb="select"] > div):is(:hover,:focus,:focus-within) {
    outline-color:var(--cv-accent)!important; border-color:var(--cv-accent)!important;
}

[class*="st-key-message_group_user_"] {width:100%!important; min-width:0; align-items:stretch;}
[class*="st-key-message_group_user_"] [data-testid="stChatMessageContent"] :is(p,li,span) {
    white-space:normal!important; overflow-wrap:normal!important; word-break:normal!important;
}
[data-testid="stPopoverBody"], [role="dialog"], [data-testid="stDialog"] > div, [data-testid="stExpanderDetails"],
.stMain [class*="st-key-sources_panel_"], .st-key-tools [data-testid="stForm"],
.st-key-appearance [data-testid="stExpanderDetails"], [data-baseweb="popover"], [data-baseweb="menu"], [role="listbox"] {
    background-color:var(--cv-surface2)!important; background-image:var(--cv-popup-gradient)!important;
    color:var(--cv-text)!important; border:1px solid var(--cv-border); border-radius:12px; box-shadow:var(--cv-popup-shadow);
}
.st-key-sources_header {background:var(--cv-surface2)!important;}
[data-testid="stPopoverBody"] {
    z-index:1000005!important; box-sizing:border-box;
    max-width:min(340px,calc(100vw - 2rem))!important; max-height:70vh!important; overflow-y:auto!important;
    padding:.75rem; border:1px solid var(--cv-accent-outline)!important;
}
[data-testid="stPopoverBody"]::backdrop {background:transparent;}
[data-baseweb="popover"]:has([data-testid="stPopoverBody"]),
[role="dialog"]:has(> [data-testid="stPopoverBody"]),
.react-aria-Popover:has([data-testid="stPopoverBody"]),
[data-rac][data-placement]:has([data-testid="stPopoverBody"]) {z-index:1000005!important;}
[data-testid="stSidebar"] [data-testid="stExpanderDetails"] {border:1px solid var(--cv-accent-outline)!important;}
[data-testid="stSidebar"] [data-testid="stExpanderDetails"]:is(:hover,:focus-within) {border-color:var(--cv-accent)!important;}

.st-key-rail {
    position:fixed!important; left:0; top:0; bottom:0; width:52px!important; height:100dvh; padding:3.5rem 5px 1rem;
    display:flex; flex-direction:column; gap:.35rem; overflow-y:auto; z-index:999900;
    background:var(--cv-surface); border-right:1px solid var(--cv-border); box-sizing:border-box;
}
.st-key-rail button {width:40px!important; min-height:38px; padding:.35rem!important; border:0!important;}
.st-key-rail button:hover {background:color-mix(in srgb,var(--cv-accent) 12%,var(--cv-surface))!important;}
body:has([data-testid="stSidebar"][aria-expanded="true"]) .st-key-rail {display:none!important;}
body:not(:has([data-testid="stSidebar"][aria-expanded="true"])) [data-testid="stMainBlockContainer"],
body:not(:has([data-testid="stSidebar"][aria-expanded="true"])) [data-testid="stBottomBlockContainer"] {padding-left:calc(1.5rem + 52px);}
.st-key-rail_bridge_host {display:none!important; height:0!important; min-height:0!important;}
[class*="st-key-notes_row_"] button {padding:.2rem .3rem; min-width:1.6rem;}
[class*="st-key-notes_row_"] {align-items:center; gap:.2rem!important;}
"""
