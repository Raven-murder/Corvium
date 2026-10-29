import json
from uuid import uuid4

import streamlit as st

SECTIONS = {
    "study_plan": ("Study plan", "event_note", None),
    "tools": ("Tools", "build", "tools"),
    "library": ("Library", "folder", "library"),
    "search": ("Search", "search", "document_search"),
    "notes": ("Notes", "edit_note", None),
    "bookmarks": ("Bookmarks", "bookmark", "bookmarks"),
    "memory": ("Memory", "psychology", "memory"),
    "tips": ("Tips", "lightbulb", "tips"),
    "pinned": ("Pinned", "push_pin", "pinned_messages"),
}

_ACK_JS = """
export default function({data, setTriggerValue}) {
    if (!data || !data.token) return;
    let finished = false;
    const finish = (opened) => {
        if (finished) return;
        finished = true;
        setTriggerValue('result', {token: data.token, opened: Boolean(opened)});
    };
    const receive = (event) => {
        const value = event.data;
        if (value && value.type === 'corvium:rail' && value.token === data.token && typeof value.opened === 'boolean') {
            finish(value.opened);
        }
    };
    window.addEventListener('message', receive);
    const timer = window.setTimeout(() => {
        const sidebar = document.querySelector('[data-testid="stSidebar"]');
        finish(sidebar?.getAttribute('aria-expanded') === 'true');
    }, 2500);
    return () => { window.removeEventListener('message', receive); window.clearTimeout(timer); };
}
"""


@st.cache_resource(show_spinner=False)
def bridge_component():
    try:
        return st.components.v2.component("corvium_sidebar_ack", js=_ACK_JS)
    except (AttributeError, RuntimeError):
        return None


def request_section(state, section):
    if section not in SECTIONS:
        raise ValueError("Choose an available section.")
    state["open_section"] = section
    key = SECTIONS[section][2]
    if key is not None:
        state[key] = True
    state.pop("rail_dialog", None)
    state["rail_request"] = {"section": section, "token": uuid4().hex}


def finish_bridge(state, result):
    request = state.get("rail_request")
    if not request or not isinstance(result, dict) or result.get("token") != request["token"]:
        return False
    if result.get("opened") is True:
        state["rail_approach"] = "js_bridge"
        state.pop("rail_dialog", None)
    else:
        state["rail_approach"] = "dialog_fallback"
        state["rail_dialog"] = request["section"]
    state.pop("rail_request", None)
    return True


def close_dialog(state):
    state.pop("rail_dialog", None)


def expansion_script(request):
    if not request or request.get("section") not in SECTIONS or len(request.get("token", "")) != 32:
        return ""
    token = json.dumps(request["token"])
    return """<script>
    (() => {
        const token = TOKEN;
        const report = (opened) => window.parent.postMessage({type:'corvium:rail', token, opened}, '*');
        window.setTimeout(() => {
            try {
                const parent = window.parent.document;
                const sidebar = parent.querySelector('[data-testid="stSidebar"]');
                if (sidebar?.getAttribute('aria-expanded') !== 'true') {
                    const control = parent.querySelector('[data-testid="stExpandSidebarButton"]');
                    if (!control) { report(false); return; }
                    control.click();
                }
                let attempts = 0;
                const timer = window.setInterval(() => {
                    const opened = parent.querySelector('[data-testid="stSidebar"]')?.getAttribute('aria-expanded') === 'true';
                    if (opened || ++attempts >= 10) { window.clearInterval(timer); report(opened); }
                }, 100);
            } catch (_) { report(false); }
        }, 200);
    })();
    </script>""".replace("TOKEN", token)
