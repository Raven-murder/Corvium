"""Run with: streamlit run app.py"""

import streamlit as st

from src.config import APP_NAME, safe_error
from src.chats import list_chats, list_messages
from src.library import list_library, list_documents, group_images_by_type, reorder_document
from src.memory import list_memories
from src.actions import study_plan_defaults, grade_quiz
from src.qa import answer_question
from src.diagrams import parse_diagram_message
from src.sources import group_answer_sources, original_download
from src import workflows as flow
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
        st.write("Reset demo replaces all current documents, memories and chats with the demo data.")
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


def show_quiz_result():
    quiz = st.session_state.get("quiz", [])
    if not quiz:
        st.info("Run the quiz tool in the sidebar to generate questions.")
        return
    version = st.session_state.get("quiz_version", 0)
    with st.form(f"quiz_form_{version}"):
        answers = []
        for number, item in enumerate(quiz, 1):
            answers.append(st.radio(
                f"{number}. {item['question']}", options=list(range(4)),
                format_func=lambda value, choices=item["options"]: choices[value],
                index=None, key=f"quiz_{version}_{number}",
            ))
        if st.form_submit_button("Check answers"):
            try:
                flow.check_quiz_answers(st.session_state, answers)
            except Exception as error:
                st.error(safe_error(error))
    checked = st.session_state.get("quiz_answers")
    if checked is not None:
        score, correctness = grade_quiz(quiz, checked)
        st.markdown(f"**Score: {score}/{len(quiz)}**")
        for number, (item, answer, correct) in enumerate(zip(quiz, checked, correctness), 1):
            st.markdown(f"**{number}. {'Correct' if correct else 'Incorrect'}**")
            st.write(f"Your answer: {item['options'][answer]}")
            st.write(f"Correct answer: {item['options'][int(item['answer_index'])]}")
            st.write(item["explanation"])
            st.caption(f"Source: [{item['source_doc']}, p.{item['source_page']}]")


def show_tool_result():
    kind = st.session_state.get("active_tool")
    if kind not in {"plan", "quiz", "diagram"}:
        return
    with st.container(border=True, key="tool_result"):
        heading, close = st.columns([8, 1])
        with heading:
            st.subheader({"plan": "Study plan", "quiz": "Quiz", "diagram": "Diagram"}[kind])
        with close:
            st.button("", icon=":material/close:", help="Close result", key="close_tool_result", on_click=run_callback, args=(flow.close_tool_result,))
        if kind == "plan":
            st.markdown(st.session_state.study_plan)
        elif kind == "quiz":
            show_quiz_result()
        else:
            result = st.session_state.diagram_result
            show_answer(result["answer"], "tool_diagram")
            show_sources(result["sources"], result["answer"], "tool_diagram")


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


def show_document(document, folders):
    document_key = f"folder_{document['folder_id']}_document_{document['id']}"
    details, action = st.columns([4, 1])
    with details:
        st.text(document["display_name"])
        st.caption(f"{document['n_chunks']} chunks")
        if document["status"] != "indexed":
            st.caption(document["status"])
    with action:
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
                title_column, delete_column = st.columns([5, 1])
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


def show_answer(content, message_key):
    diagram = parse_diagram_message(content)
    if diagram is None:
        st.markdown(content)
        return
    try:
        if diagram["language"] == "mermaid":
            render = getattr(st, "mermaid_chart", None) or getattr(st, "mermaid", None)
            if not callable(render):
                raise RuntimeError("Mermaid rendering is unavailable.")
            render(diagram["code"], width="stretch")
        else:
            st.graphviz_chart(diagram["code"], width="stretch")
    except Exception:
        st.error("The diagram could not be displayed. You can inspect its code below.")
    st.caption(diagram["caption"])
    with st.expander("View code", expanded=False, key=f"diagram_code_{message_key}"):
        st.code(diagram["code"], language=diagram["language"])


def show_sources(sources, answer, message_key):
    groups = group_answer_sources(sources, answer)
    if not groups:
        return
    with st.popover("Sources", type="tertiary", width="stretch", key=f"answer_sources_{message_key}"):
        for group in groups:
            heading, original = st.columns([3, 1])
            with heading:
                with st.expander(f"• **{group['doc_name']}**", expanded=False, key=f"source_doc_{message_key}_{group['key']}"):
                    for passage in group["passages"]:
                        st.markdown(f"**{passage['heading']}**")
                        if passage["text"]:
                            st.text(passage["text"])
                        else:
                            st.caption("No complete sentence fits in a 600-character preview.")
            with original:
                download = original_download(group)
                if download is not None:
                    st.download_button(
                        "Open original", **download, type="tertiary", on_click="ignore",
                        key=f"source_download_{message_key}_{group['key']}",
                    )


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


def show_empty_chat():
    with st.container(key="empty_chat"):
        st.markdown(
            '<div class="chat-greeting">What are we studying today?</div>'
            '<p class="chat-subtitle">Ask about your notes, build a study plan, or quiz yourself.</p>',
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
    st.caption("Example commands")
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
    st.set_page_config(page_title=APP_NAME, page_icon="📚", layout="wide", initial_sidebar_state="expanded")
    st.markdown("""
        <style>
        [data-testid="stAppViewContainer"] {
            background:radial-gradient(ellipse at 55% 5%, rgba(124,140,255,.07), transparent 55%), #0B0F19;
        }
        [data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {
            background:#7C8CFF; border-color:#7C8CFF; color:#0B0F19; border-radius:10px;
        }
        [data-testid="stBaseButton-primary"]:hover {background:#93A0FF; border-color:#93A0FF; color:#0B0F19;}
        [data-testid="stExpander"], [data-testid="stChatMessage"], [data-testid="stPopoverBody"] {border-radius:12px;}
        .st-key-selected_chat_id [data-baseweb="select"] > div {
            border-color:#7C8CFF; border-radius:10px; box-shadow:0 0 0 1px rgba(124,140,255,.12);
        }
        [class*="st-key-chat_active_"] {border-left:2px solid #7C8CFF; border-radius:8px; padding-left:.4rem; background:rgba(124,140,255,.08);}
        [data-testid="stMainBlockContainer"] {max-width:800px; margin-inline:auto; padding-top:1.5rem; padding-bottom:7rem;}
        [data-testid="stBottomBlockContainer"] {max-width:800px; margin-inline:auto;}
        [data-testid="stChatInput"] {
            max-width:760px; margin-inline:auto; border:1px solid #7C8CFF; border-radius:18px;
            background:#151B2B; color:#E6E9F2;
            box-shadow:0 0 0 1px rgba(124,140,255,.12), 0 6px 24px rgba(124,140,255,.14);
        }
        [data-testid="stChatInput"]:focus-within {box-shadow:0 0 0 2px rgba(124,140,255,.3), 0 6px 26px rgba(124,140,255,.2);}
        [data-testid="stChatInput"] textarea {background:transparent; color:#E6E9F2;}
        [data-testid="stChatMessage"] [data-testid="stPopover"] > button {
            min-height:1.75rem; padding:.1rem .25rem; justify-content:flex-start;
        }
        .st-key-empty_chat {margin-top:clamp(3rem, 16vh, 9rem);}
        .chat-greeting {text-align:center; font-size:clamp(1.65rem, 3vw, 2.1rem); font-weight:600; color:#E6E9F2;}
        .chat-subtitle {text-align:center; color:#AAB4CC; margin:.6rem 0 1.5rem; font-size:.95rem;}
        .st-key-empty_chat button {border-radius:20px; border-color:#34405D; background:#151B2B; color:#E6E9F2;}
        .st-key-empty_chat button:hover {border-color:#7C8CFF; box-shadow:0 0 12px rgba(124,140,255,.12);}
        .st-key-tips pre {font-size:.75rem; line-height:1.4;}
        [data-testid="stPopoverBody"] {border:0; padding:.5rem;}
        [data-testid="stPopoverBody"] button {border:0; padding:.15rem .35rem;}
        [data-testid="stPopoverBody"] [data-baseweb="input"],
        [data-testid="stPopoverBody"] [data-baseweb="select"] > div,
        .st-key-manage_chats [data-baseweb="input"] {border:0; box-shadow:none;}
        [data-testid="stPopoverBody"] [data-testid="stFileUploaderDropzone"] {border:0; padding:.25rem; background:transparent;}
        .st-key-library_controls button {border:0; padding:.15rem .35rem;}
        .st-key-sidebar_footer {margin-top:auto; padding-top:1.5rem;}
        [data-testid="stSidebarUserContent"] > [data-testid="stVerticalBlock"] {min-height:calc(100dvh - 5rem);}
        </style>
    """, unsafe_allow_html=True)
    try:
        bind_workspace(st.query_params, st.session_state)
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
        st.markdown(f"**{APP_NAME}**")
        st.button("New chat", icon=":material/add_comment:", type="primary", width="stretch", key="new_chat", on_click=run_callback, args=(flow.new_chat,))
        if st.session_state.get("sidebar_error"):
            st.error(st.session_state.pop("sidebar_error"))
        try:
            chats = flow.initialize_chat_selection(st.session_state, list_chats())
            titles = {chat["id"]: chat["title"] for chat in chats}
            chat_id = st.selectbox("Chat", list(titles), format_func=titles.get, key="selected_chat_id", label_visibility="collapsed")
            show_manage_chats(chats)
        except Exception as error:
            st.error(safe_error(error))
            st.stop()
        with st.expander("Tools", icon=":material/build:", expanded=False, key="tools"):
            pending = show_actions_panel(documents, memories)
        with st.expander("Library", icon=":material/folder:", expanded=False, key="library"):
            for result in demo_results:
                if result["error"] or result["status"] != "indexed":
                    st.warning(f"{result['name']}: {result['error'] or result['status']}")
            show_library_panel(library)
        with st.expander("Memory", icon=":material/psychology:", expanded=False, key="memory"):
            show_memory_panel(memories)
        with st.expander("Tips", icon=":material/lightbulb:", expanded=False, key="tips"):
            show_tips()
        with st.container(key="sidebar_footer"):
            st.caption("Answers are generated by Nebius-hosted NVIDIA Nemotron; text you upload is sent to the API.")
            st.button("Reset demo", key="reset_demo", on_click=run_callback,
                      args=(flow.request_destructive_action, "demo", "Reset demo"))
    if st.session_state.get("destructive_confirmation"):
        show_delete_dialog()
    starter_question, starter_tool = flow.consume_starter(st.session_state)
    pending = pending or starter_tool
    if pending:
        try:
            with st.spinner({"plan": "Generating study plan…", "quiz": "Generating quiz…", "diagram": "Generating diagram…"}[pending[0]]):
                flow.run_tool(st.session_state, *pending)
        except Exception as error:
            st.error(safe_error(error))
    show_tool_result()
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
        with st.chat_message(message["role"]):
            if message.get("error"):
                st.error(message["content"])
            elif message["role"] == "assistant":
                show_answer(message["content"], f"chat_{chat_id}_message_{message['id']}")
            else:
                st.markdown(message["content"])
            if message["role"] == "assistant":
                show_sources(message.get("sources", []), message["content"], f"chat_{chat_id}_message_{message['id']}")
    if question:
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
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


if __name__ == "__main__":
    main()
