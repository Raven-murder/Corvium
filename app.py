"""Run with: streamlit run app.py"""

import streamlit as st

from src.config import APP_NAME, safe_error
from src.chats import list_chats, list_messages, list_pinned_messages, chat_label, pinned_message_label
from src.library import list_library, list_documents, group_images_by_type, reorder_document
from src.memory import list_memories
from src.actions import study_plan_defaults, grade_quiz
from src.plans import list_plans
from src.qa import answer_question
from src.diagrams import parse_diagram_message, presentation_code
from src.sources import (
    group_answer_sources, original_download, open_sources_panel, active_sources_panel,
    dock_sources_panel, sources_panel_dock, close_sources_panel,
)
from src import workflows as flow
from src import viewer as doc_viewer
from src import search as doc_search
from src import notes, themes
from src.branding import load_logo, DEFAULT_ICON
from src.workspaces import bind_workspace
from src.usage import UsageLimitError


def run_callback(action, *args):
    try:
        _, unchanged = bind_workspace(st.query_params, st.session_state)
        if not unchanged:
            return
        action(st.session_state, *args)
    except Exception as error:
        st.session_state.sidebar_error = safe_error(error)


@st.dialog("Confirm deletion", dismissible=False)
def show_delete_dialog():
    request = st.session_state.get("destructive_confirmation")
    if request is None:
        st.rerun()
    kind = request["kind"]
    st.text(request["name"])
    if kind == "folder":
        st.write("Delete this folder? Its documents move to unfiled and are not deleted.")
    elif kind == "demo":
        st.write("Reset demo replaces all current documents, memories and chats with the demo data and removes saved study plans, notes and bookmarks.")
    elif kind == "memories":
        st.write("Delete all saved memories?")
    else:
        st.write(f"Delete this {kind}?")
    if st.session_state.get("sidebar_error"):
        st.error(st.session_state.pop("sidebar_error"))
    delete, cancel = st.columns(2)
    delete.button(
        "Delete", type="primary", key=f"confirm_destructive_{request['token']}",
        on_click=run_callback, args=(flow.confirm_destructive_action, request["token"]),
    )
    cancel.button(
        "Cancel", key=f"cancel_destructive_{request['token']}",
        on_click=run_callback, args=(flow.cancel_destructive_action, request["token"]),
    )


def show_actions_panel(documents, memories):
    pending = None
    defaults = study_plan_defaults(memories)
    ids = [document["id"] for document in documents]
    labels = {document["id"]: document["display_name"] for document in documents}
    st.markdown("**Study plan**")
    days = st.number_input("Number of days", min_value=1, value=defaults["days"], step=1, key="plan_days")
    topics = list(dict.fromkeys(defaults["focus_topics"] + st.session_state.get("custom_focus_topics", [])))
    focus = st.multiselect("Focus topics", topics, default=defaults["focus_topics"], key="plan_focus_topics")
    st.text_input("Add a focus topic", key="new_focus_topic", on_change=run_callback, args=(flow.add_focus_topic,))
    plan_sources = st.multiselect("Source documents", ids, default=ids, format_func=labels.get, key="plan_sources")
    if st.button("Run", key="run_plan", disabled=not plan_sources):
        pending = ("plan", {"days": int(days), "focus_topics": focus, "source_doc_ids": plan_sources})
    st.divider()
    st.markdown("**Quiz**")
    count = st.number_input("Number of questions", min_value=3, max_value=10, value=5, key="quiz_count")
    topic = st.text_input("Topic (optional)", key="quiz_topic")
    difficulty = st.selectbox("Difficulty", ["easy", "medium", "hard"], index=1, key="quiz_difficulty")
    quiz_sources = st.multiselect("Source documents", ids, default=ids, format_func=labels.get, key="quiz_sources")
    if st.button("Run", key="run_quiz", disabled=not quiz_sources):
        pending = ("quiz", {"n": int(count), "topic": topic.strip() or None, "difficulty": difficulty, "source_doc_ids": quiz_sources})
    st.divider()
    st.markdown("**Diagram**")
    diagram_kind = st.selectbox("Type", ["flowchart", "mind map", "sequence"], key="diagram_kind")
    diagram_topic = st.text_input("Topic", key="diagram_topic")
    diagram_sources = st.multiselect("Source documents", ids, default=ids, format_func=labels.get, key="diagram_sources")
    if st.button("Run", key="run_diagram", disabled=not diagram_sources or not diagram_topic.strip()):
        pending = ("diagram", {"question": diagram_topic.strip(), "kind": diagram_kind, "doc_ids": diagram_sources})
    return pending


def show_quiz_result(message, scope="chat"):
    quiz = flow.quiz_for_message(message)
    selections = flow.quiz_selections(st.session_state, message)
    for number, item in enumerate(quiz, 1):
        key = flow.quiz_choice_key(message, number, scope)
        st.radio(
            f"{number}. {item['question']}", options=list(range(4)),
            format_func=lambda value, choices=item["options"]: choices[value],
            index=None if key in st.session_state else selections[number - 1], key=key,
            on_change=run_callback, args=(flow.choose_quiz_answer, message["id"], number - 1, key, message["chat_id"]),
        )
    if st.button("Check answers", key=f"check_quiz_{scope}_{message['id']}"):
        try:
            flow.check_quiz_answers(st.session_state, message["id"], message["chat_id"])
        except Exception as error:
            st.error(safe_error(error))
    checked = st.session_state.get("quiz_answers", {}).get(message["id"])
    if checked is not None:
        score, correctness = grade_quiz(quiz, checked)
        st.markdown(f"**Score: {score}/{len(quiz)}**")
        for number, (item, answer, correct) in enumerate(zip(quiz, checked, correctness), 1):
            st.markdown(f"**{number}. {'Correct' if correct else 'Incorrect'}**")
            st.write(f"Your answer: {item['options'][answer]}")
            st.write(f"Correct answer: {item['options'][int(item['answer_index'])]}")
            st.write(item["explanation"])
            st.caption(f"Source: [{item['source_doc']}, p.{item['source_page']}]")


def show_plan_view():
    st.button("Back to chat", icon=":material/arrow_back:", key="back_to_chat",
              on_click=run_callback, args=(flow.open_chat_view,))
    st.subheader("Study plan")
    plans = list_plans()
    flow.initialize_plan_selection(st.session_state, plans)
    if not plans:
        st.info("No study plans saved yet. Generate one in Tools.")
        st.button("Open Tools", icon=":material/build:", key="open_plan_tools",
                  on_click=run_callback, args=(flow.open_tools,))
        return
    by_id = {plan["id"]: plan for plan in plans}
    plan_id = st.selectbox(
        "Saved plans", list(by_id), key="selected_plan_id",
        format_func=lambda value: ("Pinned: " if by_id[value]["pinned"] else "") + by_id[value]["title"]
        + " · " + by_id[value]["created_at"][:19].replace("T", " ") + " UTC",
    )
    plan = by_id[plan_id]
    key = f"saved_plan_title_{plan_id}"
    st.text_input("Plan title", value=plan["title"], max_chars=120, key=key,
                  on_change=run_callback, args=(flow.rename_plan_from_input, plan_id, key, plan["title"]))
    with st.container(horizontal=True, gap="small", key="saved_plan_controls"):
        st.button("Regenerate", icon=":material/refresh:", key=f"regenerate_plan_{plan_id}",
                  on_click=run_callback, args=(flow.queue_plan_regeneration, plan_id))
        st.button("Unpin" if plan["pinned"] else "Pin", icon=":material/push_pin:", key=f"pin_plan_{plan_id}",
                  on_click=run_callback, args=(flow.set_plan_pinned, plan_id, not plan["pinned"]))
        st.button("", icon=":material/delete:", help="Delete plan", type="tertiary", key=f"delete_plan_{plan_id}",
                  on_click=run_callback, args=(flow.request_destructive_action, "plan", plan["title"], plan_id))
    st.caption(f"{plan['days']} days · {plan['created_at'][:10]}")
    st.markdown(plan["body"]["markdown"])


def show_chat_message(message, chat_id, scope="chat"):
    with st.chat_message(message["role"], width="stretch"):
        if message.get("error"):
            st.error(message["content"])
        elif message.get("kind") == "quiz":
            show_quiz_result(message, scope)
        elif message.get("kind") == "plan":
            st.markdown("**Study plan**")
            st.markdown(message["content"])
        elif message["role"] == "assistant":
            show_answer(message["content"], f"{scope}_{chat_id}_message_{message['id']}")
        else:
            st.markdown(message["content"])
        with st.container(horizontal=True, gap="small", key=f"message_actions_{scope}_{chat_id}_{message['id']}"):
            show_message_pin(message, scope)
            if message["role"] == "assistant":
                st.button(
                    "", icon=":material/note_add:", help="Save to notes", type="tertiary",
                    key=f"save_note_{scope}_{chat_id}_{message['id']}", on_click=run_callback,
                    args=(flow.save_answer_to_notes, chat_id, message["id"]),
                )
            if message["role"] == "assistant" and message.get("kind") != "quiz":
                show_sources(message.get("sources", []), message["content"], f"{scope}_{chat_id}_message_{message['id']}")
        if st.session_state.get("note_saved_message") == (chat_id, message["id"]):
            st.caption("Saved to notes.")


def show_message_pin(message, scope="chat"):
    pinned = bool(message.get("pinned"))
    marker = "on" if pinned else "off"
    st.button(
        "", icon=":material/push_pin:", help="Unpin message" if pinned else "Pin message", type="tertiary",
        key=f"pin_{marker}_{scope}_message_{message['chat_id']}_{message['id']}",
        on_click=run_callback, args=(flow.pin_message_from_view, message["chat_id"], message["id"], not pinned),
    )


def show_memory_panel(memories):
    for memory in memories:
        details, remove = st.columns([5, 1])
        with details:
            st.caption(memory["kind"])
            key = f"memory_text_{memory['id']}"
            st.text_input(
                "Memory text", value=memory["text"], key=key, label_visibility="collapsed",
                on_change=run_callback, args=(flow.edit_memory_from_input, memory["id"], key),
            )
        with remove:
            st.button("", icon=":material/delete:", help="Delete memory", type="tertiary", key=f"memory_delete_{memory['id']}",
                      on_click=run_callback, args=(flow.request_destructive_action, "memory", memory["text"], memory["id"]))
    with st.popover("", icon=":material/add:", help="Add memory", type="tertiary", key="add_memory_popover"):
        with st.form("add_memory_form", border=False):
            st.text_input("Memory text", key="new_memory_text")
            st.selectbox("Kind", ["fact", "deadline", "weak_topic", "preference"], key="new_memory_kind")
            st.form_submit_button("", icon=":material/check:", help="Add memory", on_click=run_callback, args=(flow.add_memory_from_input,))
    st.button(
        "Clear all", key="clear_memories", disabled=not memories,
        on_click=run_callback, args=(flow.request_destructive_action, "memories", "All memories"),
    )



def show_notes_panel():
    with st.popover("New note", icon=":material/note_add:", type="tertiary", key="new_note_popover"):
        with st.form("new_note_form", border=False):
            st.text_input("Title", max_chars=120, key="new_note_title")
            st.text_area("Note text", max_chars=20_000, key="new_note_body")
            st.form_submit_button("Create note", on_click=run_callback, args=(flow.create_note_from_input,))
    query = st.text_input("Filter notes", key="notes_filter")
    items = notes.list_notes(query)
    if not items:
        st.caption("No matching notes." if query.strip() else "Save an answer or create a note here.")
    for note in items:
        note_key = f"note_{note['id']}_{note['updated_at']}"
        st.text(note["title"])
        if note["doc_name"]:
            st.caption(note["doc_name"])
        st.markdown(note["body"])
        with st.container(horizontal=True, gap="small", key=f"note_controls_{note['id']}"):
            with st.popover("", icon=":material/edit:", help="Edit note", type="tertiary", key=f"edit_{note_key}"):
                with st.form(f"edit_form_{note_key}", border=False):
                    title_key, body_key = f"title_{note_key}", f"body_{note_key}"
                    st.text_input("Title", value=note["title"], max_chars=120, key=title_key)
                    st.text_area("Note text", value=note["body"], max_chars=20_000, key=body_key)
                    st.form_submit_button("Save", on_click=run_callback,
                                          args=(flow.update_note_from_input, note["id"], title_key, body_key))
            marker = "on" if note["pinned"] else "off"
            st.button("", icon=":material/push_pin:", help="Unpin note" if note["pinned"] else "Pin note", type="tertiary",
                      key=f"pin_{marker}_note_{note['id']}", on_click=run_callback,
                      args=(flow.pin_note_from_sidebar, note["id"], not note["pinned"]))
            st.button("", icon=":material/delete:", help="Delete note", type="tertiary", key=f"delete_note_{note['id']}",
                      on_click=run_callback, args=(flow.request_destructive_action, "note", note["title"], note["id"]))


def show_bookmarks_panel():
    bookmarks = notes.list_bookmarks()
    if not bookmarks:
        st.caption("Open a document to bookmark a page, slide or section.")
    previous_doc = None
    for bookmark in bookmarks:
        if previous_doc != bookmark["doc_id"]:
            st.text(bookmark["doc_name"])
            previous_doc = bookmark["doc_id"]
        st.button(bookmark["label"], type="tertiary", width="stretch", key=f"open_bookmark_{bookmark['id']}",
                  on_click=run_callback, args=(flow.open_bookmark, bookmark["id"]))
        st.caption(notes.locator_label(bookmark["doc_kind"], bookmark["locator"]))
        with st.container(horizontal=True, gap="small", key=f"bookmark_controls_{bookmark['id']}"):
            with st.popover("", icon=":material/edit:", help="Rename bookmark", type="tertiary", key=f"edit_bookmark_{bookmark['id']}"):
                key = f"bookmark_label_{bookmark['id']}_{bookmark['label']}"
                st.text_input("Label", value=bookmark["label"], max_chars=120, key=key,
                              on_change=run_callback, args=(flow.rename_bookmark_from_input, bookmark["id"], key))
            st.button("", icon=":material/delete:", help="Delete bookmark", type="tertiary", key=f"delete_bookmark_{bookmark['id']}",
                      on_click=run_callback, args=(flow.request_destructive_action, "bookmark", bookmark["label"], bookmark["id"]))


def show_viewer_bookmark(document, locator):
    saved = notes.current_bookmark(document["id"], locator)
    if saved:
        st.caption("Bookmarked")
    with st.popover("Add bookmark", icon=":material/bookmark:", type="tertiary", key=f"viewer_bookmark_{document['id']}"):
        with st.form(f"viewer_bookmark_form_{document['id']}", border=False):
            key = f"viewer_bookmark_label_{document['id']}"
            st.text_input("Label (optional)", max_chars=120, key=key)
            st.form_submit_button("Add bookmark", disabled=saved is not None, on_click=run_callback,
                                  args=(doc_viewer.add_current_bookmark, key))
    if st.session_state.get("viewer_notice"):
        st.success(st.session_state.pop("viewer_notice"))
    if st.session_state.get("sidebar_error"):
        st.error(st.session_state.pop("sidebar_error"))


def show_document(document, folders):
    document_key = f"folder_{document['folder_id']}_document_{document['id']}"
    details, action = st.columns([4, 1])
    with details:
        st.text(document["display_name"])
        st.caption(f"{document['n_chunks']} chunks")
        if document["status"] != "indexed":
            st.caption(document["status"])
    with action:
        st.button("", icon=":material/visibility:", help="Open document", type="tertiary", key=f"view_{document_key}",
                  on_click=run_callback, args=(doc_viewer.request_viewer, document["id"], {"page": 1}))
        with st.popover("", icon=":material/edit:", help="Edit document", type="tertiary", key=f"edit_{document_key}"):
            with st.form(f"document_edit_{document_key}", border=False):
                display_name = st.text_input("Display name", value=document["display_name"], key=f"name_{document_key}")
                options = [None] + [folder["id"] for folder in folders]
                labels = {folder["id"]: folder["name"] for folder in folders}
                folder_id = st.selectbox(
                    "Folder", options, index=options.index(document["folder_id"]),
                    format_func=lambda value: "Unfiled" if value is None else labels[value],
                    key=f"destination_{document_key}",
                )
                if st.form_submit_button("", icon=":material/check:", help="Save changes", key=f"save_{document_key}"):
                    try:
                        flow.edit_document(st.session_state, document["id"], display_name, folder_id)
                    except Exception as error:
                        st.error(safe_error(error))
                    else:
                        st.rerun()
            up, down, remove = st.columns(3)
            direction = None
            if up.button("", icon=":material/arrow_upward:", help="Move up", type="tertiary", key=f"up_{document_key}"):
                direction = "up"
            if down.button("", icon=":material/arrow_downward:", help="Move down", type="tertiary", key=f"down_{document_key}"):
                direction = "down"
            remove.button(
                "", icon=":material/delete:", help="Delete document",
                type="tertiary", key=f"delete_{document_key}",
                on_click=run_callback, args=(flow.request_destructive_action, "document", document["display_name"],
                                            {"document": document, "document_key": document_key}),
            )
            if direction:
                try:
                    reorder_document(document["id"], direction)
                except Exception as error:
                    st.error(safe_error(error))
                else:
                    st.rerun()


def show_folder(folder, folders):
    with st.expander(f"{folder['name']} ({len(folder['documents'])})", expanded=False, key=f"library_folder_{folder['id']}"):
        with st.popover("", icon=":material/edit:", help="Edit folder", type="tertiary", key=f"folder_edit_{folder['id']}"):
            key = f"folder_name_{folder['id']}"
            st.text_input(
                "Folder name", value=folder["name"], key=key, label_visibility="collapsed",
                on_change=run_callback, args=(flow.rename_folder_from_input, folder["id"], key, folder["name"]),
            )
            st.button(
                "", icon=":material/delete:", help="Delete folder; move its documents to Unfiled",
                type="tertiary", key=f"folder_delete_{folder['id']}",
                on_click=run_callback, args=(flow.request_destructive_action, "folder", folder["name"], folder["id"]),
            )
        for document in folder["documents"]:
            show_document(document, folders)


def show_manage_chats(chats):
    with st.expander("Manage chats", icon=":material/forum:", expanded=False, key="manage_chats"):
        for chat in chats:
            row = "chat_active" if chat["id"] == st.session_state.get("selected_chat_id") else "chat_item"
            with st.container(key=f"{row}_{chat['id']}"):
                pin_column, title_column, delete_column = st.columns([1, 5, 1])
                with pin_column:
                    pinned = bool(chat.get("pinned"))
                    marker = "on" if pinned else "off"
                    st.button(
                        "", icon=":material/push_pin:", help="Unpin chat" if pinned else "Pin chat", type="tertiary",
                        key=f"pin_{marker}_chat_{chat['id']}", on_click=run_callback,
                        args=(flow.pin_chat_from_sidebar, chat["id"], not pinned),
                    )
                with title_column:
                    key = f"chat_title_{chat['id']}_{chat['updated_at']}"
                    st.text_input(
                        "Chat title", value=chat["title"], max_chars=40, key=key, label_visibility="collapsed",
                        on_change=run_callback, args=(flow.rename_chat_from_input, chat["id"], key, chat["title"]),
                    )
                with delete_column:
                    st.button(
                        "", icon=":material/delete:", help="Delete chat",
                        type="tertiary", key=f"chat_delete_{chat['id']}",
                        on_click=run_callback, args=(flow.request_destructive_action, "chat", chat["title"], chat["id"]),
                    )


def show_pinned_panel():
    with st.expander("Pinned", icon=":material/push_pin:", expanded=False, key="pinned_messages"):
        messages = list_pinned_messages()
        if not messages:
            st.caption("Pin a message to keep it here.")
        for message in messages:
            st.button(
                pinned_message_label(message), type="tertiary", width="stretch", wrap=True,
                key=f"pinned_message_{message['chat_id']}_{message['id']}", on_click=run_callback,
                args=(flow.open_pinned_message, message["chat_id"], message["id"]),
            )
            st.caption(message["chat_title"])


def dismiss_pinned_message():
    run_callback(flow.close_pinned_message)


@st.dialog("Pinned message", width="large", on_dismiss=dismiss_pinned_message)
def show_pinned_message_dialog():
    if any(st.session_state.get(key) for key in ("viewer_request", "diagram_view", "sources_panel", "destructive_confirmation")):
        flow.close_pinned_message(st.session_state)
        st.rerun()
        return
    message = flow.active_pinned_message(st.session_state)
    if message is None:
        st.rerun()
        return
    st.caption(message["chat_title"])
    st.button("Go to chat", icon=":material/forum:", type="primary", key="pinned_go_to_chat",
              on_click=run_callback, args=(flow.go_to_pinned_chat,))
    show_chat_message(message, message["chat_id"], scope="pinned")
    st.button("Close", key="close_pinned_message", on_click=run_callback, args=(flow.close_pinned_message,))


def show_diagram_chart(diagram, message_key, enlarged=False):
    size = "large" if enlarged else "inline"
    with st.container(key=f"diagram_canvas_{size}_{message_key}", border=False, height="content" if enlarged else 420):
        try:
            code = presentation_code(diagram, st.session_state.get("theme_preset", "raven"), st.session_state.get("accent_name", "indigo"))
            if diagram["language"] == "mermaid":
                render = getattr(st, "mermaid_chart", None) or getattr(st, "mermaid", None)
                if not callable(render):
                    raise RuntimeError("Mermaid rendering is unavailable.")
                render(code, width="content")
            else:
                st.graphviz_chart(code, width="content", height="content")
        except Exception:
            st.error("The diagram could not be displayed. You can inspect its code below.")


def dismiss_diagram_view():
    run_callback(flow.close_diagram_view)


@st.dialog("Diagram", width="large", on_dismiss=dismiss_diagram_view)
def show_diagram_dialog():
    diagram = flow.active_diagram_view(st.session_state)
    if diagram is None:
        st.rerun()
        return
    show_diagram_chart(diagram, diagram["message_key"], enlarged=True)
    st.caption(diagram["caption"])
    with st.expander("View code", expanded=False, key="enlarged_diagram_code"):
        st.code(diagram["code"], language=diagram["language"])
    st.button("Close", key="close_diagram_view", on_click=run_callback, args=(flow.close_diagram_view,))


def show_answer(content, message_key):
    diagram = parse_diagram_message(content)
    if diagram is None:
        st.markdown(content)
        return
    show_diagram_chart(diagram, message_key)
    st.caption(diagram["caption"])
    st.button(
        "Enlarge", icon=":material/open_in_full:", type="tertiary", key=f"enlarge_diagram_{message_key}",
        on_click=run_callback, args=(flow.open_diagram_view, content, message_key),
    )
    with st.expander("View code", expanded=False, key=f"diagram_code_{message_key}"):
        st.code(diagram["code"], language=diagram["language"])


def show_source_groups(groups, message_key):
    for group in groups:
        heading, original = st.columns([3, 1])
        with heading:
            with st.expander(f"• **{group['doc_name']}**", expanded=False, key=f"source_doc_panel_{message_key}_{group['key']}"):
                for passage_index, passage in enumerate(group["passages"]):
                    st.markdown(f"**{passage['heading']}**")
                    if group.get("doc_id") is not None:
                        st.button(
                            "", icon=":material/visibility:", help="Open passage", type="tertiary",
                            key=f"source_open_{message_key}_{group['key']}_{passage_index}",
                            on_click=run_callback, args=(doc_viewer.request_viewer, group["doc_id"], passage["locator"]),
                        )
                    if passage["text"]:
                        st.text(passage["text"])
                    else:
                        st.caption("No complete sentence fits in a 600-character preview.")
        with original:
            download = original_download(group)
            if download is not None:
                st.download_button(
                    "Open original", **download, type="tertiary", on_click="ignore",
                    key=f"source_download_panel_{message_key}_{group['key']}",
                )


def show_sources(sources, answer, message_key):
    if not group_answer_sources(sources, answer):
        return
    st.button(
        "Sources", icon=":material/menu_book:", type="tertiary", key=f"answer_sources_{message_key}",
        on_click=run_callback, args=(open_sources_panel, sources, answer, message_key),
    )


def show_sources_panel():
    panel = active_sources_panel(st.session_state)
    if panel is None:
        return
    dock = sources_panel_dock(st.session_state)
    groups = group_answer_sources(panel["sources"], panel["answer"])
    with st.container(key=f"sources_panel_{dock}", border=False):
        with st.container(key="sources_header", horizontal=True, gap="small"):
            st.markdown("**Sources**", width="stretch")
            for side, icon in (("left", "align_horizontal_left"), ("right", "align_horizontal_right"), ("bottom", "vertical_align_bottom")):
                st.button(
                    "", icon=f":material/{icon}:", help=f"Dock {side}", type="tertiary", key=f"sources_dock_{side}",
                    on_click=run_callback, args=(dock_sources_panel, side),
                )
            st.button(
                "", icon=":material/close:", help="Close Sources", type="tertiary", key="close_sources_panel",
                on_click=run_callback, args=(close_sources_panel,),
            )
        show_source_groups(groups, panel["message_key"])


def dismiss_document_viewer():
    run_callback(doc_viewer.close_viewer)


def show_viewer_parts(data, locator):
    parts = doc_viewer.selected_parts(data, locator)
    if not parts or not any(part["text"].strip() for part in parts):
        st.info("No extracted text is available at this location.")
    for part in parts:
        st.text(part["heading"])
        st.text(part["text"])


@st.dialog("Document", width="large", on_dismiss=dismiss_document_viewer)
def show_document_viewer():
    request = doc_viewer.active_viewer(st.session_state)
    if request is None:
        st.rerun()
        return
    try:
        data = doc_viewer.load_document(request["doc_id"])
        document = data["document"]
        locator = doc_viewer.normalize_locator(data, request["locator"])
        kind = document["kind"]
        st.text(document["display_name"])
        show_viewer_bookmark(document, locator)
        download = original_download(document)
        if download is not None:
            st.download_button("Open original", **download, on_click="ignore", key=f"viewer_original_{document['id']}")
        if data["notice"]:
            st.caption(data["notice"])
        if kind in {"docx", "pptx"}:
            st.caption("Layout is simplified. Open original downloads the exact file.")
        revision = request["revision"]
        if kind == "pdf":
            previous, number, following = st.columns([1, 3, 1])
            previous.button("", icon=":material/chevron_left:", help="Previous page", disabled=locator["page"] <= 1,
                            key=f"viewer_previous_{revision}", on_click=run_callback, args=(doc_viewer.move_page, -1))
            key = f"viewer_page_{document['id']}_{revision}"
            number.number_input("Page", min_value=1, max_value=data["page_count"], value=locator["page"], step=1, key=key,
                                on_change=run_callback, args=(doc_viewer.choose_page, key))
            following.button("", icon=":material/chevron_right:", help="Next page", disabled=locator["page"] >= data["page_count"],
                             key=f"viewer_next_{revision}", on_click=run_callback, args=(doc_viewer.move_page, 1))
            st.caption(f"Page {locator['page']} of {data['page_count']}")
            try:
                rendered = doc_viewer.render_pdf_page(document["id"], locator["page"])
            except Exception:
                rendered = None
            if rendered:
                st.image(rendered["png"], width="stretch")
            else:
                show_viewer_parts(data, locator)
        elif kind == "image":
            st.caption("Page 1 of 1")
            image, text = st.columns([3, 2])
            with image:
                original = doc_viewer.image_original(document["id"])
                if original is not None:
                    st.image(original, width="stretch")
                else:
                    st.caption("The image preview is unavailable. Extracted text is shown beside it.")
            with text:
                with st.expander("Extracted OCR text", expanded=True, key=f"viewer_ocr_{document['id']}"):
                    show_viewer_parts(data, locator)
        else:
            if data["parts"]:
                key = f"viewer_section_{document['id']}_{revision}"
                st.selectbox(
                    "Slide" if kind == "pptx" else "Section", list(range(len(data["parts"]))),
                    index=doc_viewer.selected_part_index(data, locator),
                    format_func=lambda index: data["parts"][index]["heading"], key=key,
                    on_change=run_callback, args=(doc_viewer.choose_section, key),
                )
            show_viewer_parts(data, locator)
    except Exception:
        st.error("This document could not be opened. It may no longer be available.")
    st.button("Close", key="close_document_viewer", on_click=run_callback, args=(doc_viewer.close_viewer,))


def show_active_dialog():
    if st.session_state.get("destructive_confirmation"):
        show_delete_dialog()
    elif doc_viewer.active_viewer(st.session_state) is not None:
        show_document_viewer()
    elif flow.active_diagram_view(st.session_state) is not None:
        show_diagram_dialog()
    elif flow.active_pinned_message(st.session_state) is not None:
        show_pinned_message_dialog()


def show_search_panel():
    with st.form("document_search_form", border=False):
        st.text_input("Search documents", placeholder="Document name or topic", key="document_search_query")
        st.form_submit_button("Search", icon=":material/search:", on_click=run_callback, args=(doc_search.queue_search,))
    if "pending_document_search" in st.session_state:
        with st.spinner("Searching documents…"):
            doc_search.run_pending_search(st.session_state)
    results = doc_search.search_results(st.session_state)
    if results is None:
        return
    if results["notice"]:
        st.caption(results["notice"])
    if not results["documents"] and results["query"]:
        st.caption("No matching documents found.")
    for result in results["documents"]:
        st.markdown(f"**{result['display_name']}**")
        st.caption(result["folder_name"])
        for passage in result["passages"]:
            st.caption(passage["heading"])
            if passage["text"]:
                st.text(passage["text"])
        opened, original = st.columns([1, 2])
        opened.button("Open", icon=":material/visibility:", type="tertiary", key=f"search_open_{result['id']}",
                      on_click=run_callback, args=(doc_viewer.request_viewer, result["id"], result["locator"]))
        download = original_download(result)
        if download is not None:
            original.download_button("Open original", **download, type="tertiary", on_click="ignore",
                                     key=f"search_original_{result['id']}")


def show_library_panel(library):
    with st.container(key="library_controls", border=False, horizontal=True, gap="small"):
        with st.popover("", icon=":material/upload_file:", help="Upload files", type="tertiary", key="library_upload"):
            uploads = st.file_uploader(
                "Upload files", type=None, accept_multiple_files=True, label_visibility="collapsed",
                help="PDF, DOCX, PPTX, text, HTML, or images. Maximum 25 MB per file.",
                key=f"uploads_{st.session_state.get('uploader_version', 0)}",
            )
        with st.popover("", icon=":material/create_new_folder:", help="New folder", type="tertiary", key="library_new_folder"):
            st.text_input("Folder name", placeholder="Folder name", label_visibility="collapsed", key="new_folder_name",
                          on_change=run_callback, args=(flow.create_folder_from_input,))
    with st.spinner("Indexing files…"):
        events = flow.process_uploads(uploads, st.session_state)
    for event in events:
        getattr(st, event["kind"])(event["message"])
    failed = [(name, entry) for name, entry in st.session_state.get("processed_files", {}).items() if entry.get("error")]
    for name, entry in failed:
        st.error(f"Couldn't index {name}: {entry['error']}")
    if failed:
        st.button("Retry failed uploads", key="retry_uploads", on_click=run_callback, args=(flow.retry_failed_uploads,))
    if events:
        library = list_library()
    st.markdown("**Documents**")
    for folder in library["folders"]:
        show_folder(folder, library["folders"])
    if library["unfiled"]:
        st.caption("Unfiled")
        for document in library["unfiled"]:
            show_document(document, library["folders"])
    st.markdown("**Images**")
    for kind, images in group_images_by_type(library["images"]).items():
        with st.expander(f"{kind} ({len(images)})", expanded=False, key=f"image_group_{kind}"):
            for document in images:
                show_document(document, library["folders"])


def show_sidebar_header(logo):
    with st.container(horizontal=True, vertical_alignment="center", gap="small", key="brand_header"):
        if logo is not None:
            st.image(logo, width=28)
        st.markdown(f"**{APP_NAME}**")


def show_appearance():
    with st.popover("Appearance", icon=":material/palette:", type="tertiary", key="appearance"):
        st.radio("Theme", list(themes.PRESETS), index=None, format_func=themes.THEME_LABELS.get,
                 key="theme_preset", on_change=run_callback, args=(themes.change_appearance, st.query_params))
        st.selectbox("Accent", list(themes.ACCENTS), index=None, format_func=str.title,
                     key="accent_name", on_change=run_callback, args=(themes.change_appearance, st.query_params))


def show_empty_chat():
    with st.container(key="empty_chat"):
        with st.container(key="empty_mascot"):
            logo = load_logo(st.session_state.get("theme_preset", "raven"))
            if logo is not None:
                st.image(logo, width=72)
            else:
                st.markdown(f"**{APP_NAME}**")
        st.markdown(
            '<div class="chat-greeting">What are we studying today?</div>'
            f'<p class="chat-subtitle">Ask {APP_NAME} about your notes, build a study plan, or quiz yourself.</p>',
            unsafe_allow_html=True,
        )
        summary, plan, quiz = st.columns(3, gap="small")
        for column, label, kind in (
            (summary, "Summarize my notes", "summary"),
            (plan, "Make a study plan", "plan"),
            (quiz, "Quiz me", "quiz"),
        ):
            column.button(
                label, key=f"starter_{kind}", width="stretch", wrap=True,
                on_click=run_callback, args=(flow.queue_starter, kind),
            )


def show_tips():
    st.caption(f"Example commands for {APP_NAME}")
    for command in (
        "Summarize normalization in 5 bullet points",
        "Explain second normal form with an example from my notes",
        "What are my weak topics, and what should I review first?",
        "Compare 2NF and 3NF in a table",
        "Remember my exam is on 5 Nov",
        "Quiz me on transactions, hard",
    ):
        st.code(command, language=None, wrap_lines=True)
    st.caption("Advice")
    st.markdown(
        "- Name the document or topic you mean for sharper answers.\n"
        "- Save exam dates in Memory so your study plan counts the days left.\n"
        "- Keep one folder per subject in the Library.\n"
        "- Open Sources under an answer to check the original passage.\n"
        "- Upload clear scans; photos of handwriting work best in good light."
    )


def main():
    bind_workspace(st.query_params, st.session_state)
    theme, accent = themes.initialize_appearance(st.session_state, st.query_params)
    logo = load_logo(theme)
    st.set_page_config(page_title=APP_NAME, page_icon=logo if logo is not None else DEFAULT_ICON,
                       layout="wide", initial_sidebar_state="expanded")
    st.markdown(themes.styles(theme, accent), unsafe_allow_html=True)
    try:
        with st.spinner("Preparing your library…"):
            demo_results = flow.initialize_library()
        st.session_state.setdefault("processed_files", {})
        documents = list_documents()
        memories = list_memories()
        library = list_library()
    except Exception as error:
        st.error(safe_error(error))
        st.stop()
    with st.sidebar:
        show_sidebar_header(logo)
        st.button("New chat", icon=":material/add_comment:", type="primary", width="stretch", key="new_chat", on_click=run_callback, args=(flow.new_chat,))
        st.button("Study plan", icon=":material/event_note:", width="stretch", key="study_plan_view",
                  on_click=run_callback, args=(flow.open_plan_view,))
        if st.session_state.get("sidebar_error"):
            st.error(st.session_state.pop("sidebar_error"))
        try:
            chats = flow.initialize_chat_selection(st.session_state, list_chats())
            titles = {chat["id"]: chat_label(chat) for chat in chats}
            chat_id = st.selectbox("Chat", list(titles), format_func=titles.get, key="selected_chat_id", label_visibility="collapsed")
            show_manage_chats(chats)
            show_pinned_panel()
        except Exception as error:
            st.error(safe_error(error))
            st.stop()
        with st.expander("Tools", icon=":material/build:", expanded=False, key="tools", on_change="rerun"):
            pending = show_actions_panel(documents, memories)
        with st.expander("Library", icon=":material/folder:", expanded=False, key="library"):
            for result in demo_results:
                if result["error"] or result["status"] != "indexed":
                    st.warning(f"{result['name']}: {result['error'] or result['status']}")
            show_library_panel(library)
        with st.expander("Search", icon=":material/search:", expanded=False, key="document_search"):
            show_search_panel()
        with st.expander("Notes", icon=":material/edit_note:", expanded=False, key="notes"):
            show_notes_panel()
        with st.expander("Bookmarks", icon=":material/bookmark:", expanded=False, key="bookmarks"):
            show_bookmarks_panel()
        with st.expander("Memory", icon=":material/psychology:", expanded=False, key="memory"):
            show_memory_panel(memories)
        with st.expander("Tips", icon=":material/lightbulb:", expanded=False, key="tips"):
            show_tips()
        with st.container(key="sidebar_footer"):
            st.caption(f"{APP_NAME} answers are generated by Nebius-hosted NVIDIA Nemotron; text you upload is sent to the API.")
            st.button("Reset demo", key="reset_demo", on_click=run_callback,
                      args=(flow.request_destructive_action, "demo", "Reset demo"))
            show_appearance()
    starter_question, starter_tool = flow.consume_starter(st.session_state)
    pending = pending or starter_tool
    if pending:
        try:
            with st.spinner({"plan": "Generating study plan…", "quiz": "Generating quiz…", "diagram": "Generating diagram…"}[pending[0]]):
                flow.run_tool(st.session_state, *pending)
        except Exception as error:
            st.error(safe_error(error))
    regenerate_id = st.session_state.pop("regenerate_plan_id", None)
    if regenerate_id is not None:
        try:
            with st.spinner("Regenerating study plan…"):
                flow.regenerate_plan(st.session_state, regenerate_id)
        except Exception as error:
            st.error(safe_error(error))
    if st.session_state.get("view", "chat") == "plan":
        show_plan_view()
        show_active_dialog()
        return
    if st.session_state.get("chat_notice"):
        st.warning(st.session_state.pop("chat_notice"))
    try:
        messages = list_messages(chat_id)
    except Exception as error:
        st.error(safe_error(error))
        st.stop()
    question = st.chat_input("Ask about your notes", key="chat_prompt", disabled=not documents) or starter_question
    if not messages and not question:
        show_empty_chat()
    for message in messages:
        show_chat_message(message, chat_id)
    if question:
        with st.chat_message("user", width="stretch"):
            st.markdown(question)
        with st.chat_message("assistant", width="stretch"):
            try:
                with st.spinner("Preparing your answer…"):
                    result = answer_question(question, chat_id=chat_id)
                show_answer(result["answer"], f"chat_{chat_id}_new_{len(messages)}")
                show_sources(result["sources"], result["answer"], f"chat_{chat_id}_new_{len(messages)}")
            except UsageLimitError as error:
                st.session_state.chat_notice = safe_error(error)
            except Exception as error:
                st.error(safe_error(error))
        st.rerun()
    show_sources_panel()
    show_active_dialog()


if __name__ == "__main__":
    main()
