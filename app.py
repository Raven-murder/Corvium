"""Run with: streamlit run app.py"""

import hashlib
import re
from pathlib import Path

import streamlit as st

from src.config import PROJECT_ROOT, safe_error
from src.db import connect, delete_document, init_db
from src.ingest import ingest_file
from src.qa import answer_question
from src.memory import list_memories, delete_memory, clear_memories
from src.actions import generate_study_plan, generate_quiz, grade_quiz


def show_actions_panel():
    st.subheader("Actions")
    pending = None
    days = st.number_input("Plan days", min_value=3, max_value=14, value=7)
    if st.button("Study plan"):
        pending = ("plan", int(days), None)
    count = st.number_input("Number of questions", min_value=3, max_value=10, value=5)
    topic = st.text_input("Quiz topic (optional)")
    if st.button("Quiz"):
        pending = ("quiz", int(count), topic.strip() or None)
    return pending


def run_pending_action(pending, result_slot):
    action, count, topic = pending
    label = "Generating study plan..." if action == "plan" else "Generating quiz..."
    error_message = None
    with result_slot.container():
        with st.status(label, expanded=True) as progress:
            try:
                with st.sidebar:
                    with st.spinner(label):
                        if action == "plan":
                            result = generate_study_plan(count)
                        else:
                            result = generate_quiz(count, topic=topic)
                if action == "plan":
                    st.session_state.study_plan = result
                else:
                    st.session_state.quiz = result
                    st.session_state.quiz_version = st.session_state.get("quiz_version", 0) + 1
                    st.session_state.pop("quiz_answers", None)
                progress.update(label="Ready", state="complete", expanded=False)
            except Exception as error:
                error_message = f"Couldn't generate the {'study plan' if action == 'plan' else 'quiz'}: {safe_error(error)}"
                progress.update(label="Generation failed", state="error")
    result_slot.empty()
    with result_slot.container():
        if error_message:
            st.error(error_message)
        if action == "plan":
            show_plan_result()
        else:
            show_quiz_result()
    if not error_message:
        st.toast("Study plan ready" if action == "plan" else "Quiz ready")


def show_plan_result():
    if st.session_state.get("study_plan"):
        st.subheader("Study plan")
        st.markdown(st.session_state.study_plan)
    else:
        st.info("Click Study plan in the sidebar to generate a plan.")


def show_quiz_result():
    quiz = st.session_state.get("quiz", [])
    if not quiz:
        st.info("Click Quiz in the sidebar to generate questions.")
        return
    st.subheader("Quiz")
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
            if any(answer is None for answer in answers):
                st.error("Select an answer for every question before checking.")
            else:
                st.session_state.quiz_answers = answers
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


def show_memory_panel():
    st.subheader("Memory")
    try:
        memories = list_memories()
    except Exception as error:
        st.error(f"Couldn't read memories: {safe_error(error)}")
        return
    if not memories:
        st.caption("Tell me about your study preferences, weak topics, or deadlines.")
    for memory in memories:
        details, action = st.columns([4, 1])
        with details:
            st.caption(memory["kind"])
            st.text(memory["text"])
        with action:
            if st.button("🗑", key=f"memory_delete_{memory['id']}", help="Delete memory"):
                try:
                    delete_memory(memory["id"])
                except Exception as error:
                    st.error(f"Couldn't delete memory: {safe_error(error)}")
                else:
                    st.rerun()

    if st.button("Clear all memories", disabled=not memories):
        st.session_state.confirm_clear_memories = True
    if st.session_state.get("confirm_clear_memories", False):
        st.warning("Delete all saved memories? This cannot be undone.")
        confirm, cancel = st.columns(2)
        with confirm:
            if st.button("Confirm clear", type="primary"):
                try:
                    clear_memories()
                except Exception as error:
                    st.error(f"Couldn't clear memories: {safe_error(error)}")
                else:
                    st.session_state.confirm_clear_memories = False
                    st.rerun()
        with cancel:
            if st.button("Cancel"):
                st.session_state.confirm_clear_memories = False
                st.rerun()


def indexed_documents():
    connection = connect()
    try:
        return connection.execute(
            "SELECT name, n_chunks FROM documents ORDER BY name COLLATE NOCASE"
        ).fetchall()
    finally:
        connection.close()


def show_sources(sources, answer):
    cited_sources = [
        source for source in sources
        if re.search(
            r"\[" + re.escape(source["doc_name"]) + r", p\."
            + str(source["page"]) + r"\]", answer
        )
    ]
    if cited_sources:
        with st.expander("Sources"):
            for source in cited_sources:
                st.markdown(f"**{source['doc_name']} · page {source['page']}**")
                st.caption(f"Retrieval score: {source['score']:.4f}")
                st.text(source["text"])
                st.divider()


def main():
    st.set_page_config(
        page_title="Project Zero: Study Assistant", page_icon="📚", layout="wide"
    )
    st.title("Project Zero: Study Assistant")
    st.caption("Ask questions about your notes and check the passages behind each answer.")

    try:
        init_db()
    except Exception as error:
        st.error(f"Couldn't open the document library: {safe_error(error)}")
        st.stop()

    st.session_state.setdefault("processed_files", {})
    st.session_state.setdefault("chat_history", [])

    with st.sidebar:
        st.header("Your notes")
        uploads = st.file_uploader(
            "Upload documents", type=["pdf", "txt", "md"], accept_multiple_files=True,
            key=f"uploads_{st.session_state.get('uploader_version', 0)}",
        )
        for upload in uploads or []:
            # Strip path components from browser-supplied names before saving.
            name = Path(upload.name.replace("\\", "/")).name
            if not name or name in {".", ".."}:
                st.error("This upload has an invalid filename.")
                continue
            contents = upload.getvalue()
            fingerprint = hashlib.sha256(contents).hexdigest()
            previous = st.session_state.processed_files.get(name)
            if previous and previous["fingerprint"] == fingerprint:
                continue
            try:
                with st.spinner(f"Indexing {name}…"):
                    folder = PROJECT_ROOT / "data" / "uploads"
                    folder.mkdir(parents=True, exist_ok=True)
                    destination = folder / name
                    destination.write_bytes(contents)
                    count = ingest_file(destination)
                st.session_state.processed_files[name] = {
                    "fingerprint": fingerprint, "count": count, "error": None
                }
                st.success(f"{name}: indexed {count} chunks.")
            except Exception as error:
                message = safe_error(error)
                # Remember failed attempts too, so chat reruns don't repeat API calls.
                st.session_state.processed_files[name] = {
                    "fingerprint": fingerprint, "error": message
                }

        failed = [
            (name, entry) for name, entry in st.session_state.processed_files.items()
            if entry.get("error")
        ]
        for name, entry in failed:
            st.error(f"Couldn't index {name}: {entry['error']}")
        if failed and st.button("Retry failed uploads"):
            for name, _ in failed:
                del st.session_state.processed_files[name]
            st.rerun()

        st.divider()
        st.subheader("Indexed documents")
        try:
            documents = indexed_documents()
        except Exception as error:
            st.error(f"Couldn't read the library: {safe_error(error)}")
            st.stop()
        if documents:
            for document in documents:
                details, action = st.columns([4, 1])
                with details:
                    st.text(document["name"])
                    st.caption(f"{document['n_chunks']} chunks")
                with action:
                    if st.button(
                        "🗑", key=f"delete_{document['name']}",
                        help=f"Delete {document['name']} from the library",
                    ):
                        try:
                            delete_document(document["name"])
                            st.session_state.processed_files.pop(document["name"], None)
                            # Reset uploads so the deleted file isn't re-ingested on rerun.
                            st.session_state.uploader_version = (
                                st.session_state.get("uploader_version", 0) + 1
                            )
                        except Exception as error:
                            st.error(f"Couldn't delete the document: {safe_error(error)}")
                        else:
                            st.rerun()
        else:
            st.caption("Your library is empty.")

    if not documents:
        st.info("Upload your notes in the sidebar first, then ask a question below.")

    # Render after answering so newly saved memories appear on the same run.
    memory_panel = st.sidebar.container()
    with st.sidebar:
        st.divider()
        pending = show_actions_panel()
    plan_tab, quiz_tab, chat_tab = st.tabs(["Plan", "Quiz", "Chat"])
    with plan_tab:
        plan_slot = st.empty()
        if not pending or pending[0] != "plan":
            with plan_slot.container():
                show_plan_result()
    with quiz_tab:
        quiz_slot = st.empty()
        if not pending or pending[0] != "quiz":
            with quiz_slot.container():
                show_quiz_result()
    if pending:
        run_pending_action(pending, plan_slot if pending[0] == "plan" else quiz_slot)

    with chat_tab:
        for message in st.session_state.chat_history:
            with st.chat_message(message["role"]):
                if message.get("error"):
                    st.error(message["content"])
                else:
                    st.markdown(message["content"])
                show_sources(message.get("sources", []), message["content"])

        question = st.chat_input("Ask a question about your notes", disabled=not documents)
        if question:
            st.session_state.chat_history.append({"role": "user", "content": question})
            with st.chat_message("user"):
                st.markdown(question)
            with st.chat_message("assistant"):
                try:
                    with st.spinner("Finding passages and preparing your answer…"):
                        result = answer_question(question)
                    st.markdown(result["answer"])
                    show_sources(result["sources"], result["answer"])
                    st.session_state.chat_history.append({
                        "role": "assistant", "content": result["answer"],
                        "sources": result["sources"],
                    })
                except Exception as error:
                    message = f"Couldn't answer this question: {safe_error(error)}"
                    st.error(message)
                    st.session_state.chat_history.append({
                        "role": "assistant", "content": message, "error": True,
                    })

    with memory_panel:
        st.divider()
        show_memory_panel()


if __name__ == "__main__":
    main()
