import hashlib
from io import BytesIO
import re

import streamlit as st
from pypdf import PdfReader

from . import perf
from .db import database_session, current_workspace_id, get_file_bytes
from .extractors import extract_docx, extract_pptx, extract_html, extract_text
from .workspaces import get_storage


def _record(doc_id):
    if type(doc_id) is not int or doc_id < 1:
        raise ValueError("Choose a valid document.")
    with database_session() as connection:
        row = connection.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            raise ValueError("This document is no longer available.")
        chunks = [dict(chunk) for chunk in connection.execute(
            "SELECT page, section, text FROM chunks WHERE doc_id = ? ORDER BY page, id", (doc_id,)
        )]
    return dict(row), chunks


def _original(document):
    return get_file_bytes(current_workspace_id(), document["id"])


def _visible(text):
    return "\n".join(part["text"] for part in extract_html(text))


def _merge_overlap(first, second):
    """Join indexed windows without repeating their shared character overlap."""
    for size in range(min(len(first), len(second)), 19, -1):
        if first.endswith(second[:size]):
            return first + second[size:]
    return first + "\n\n" + second


def _parts(chunks, kind):
    groups = {}
    for chunk in chunks:
        page = max(1, int(chunk.get("page") or 1))
        section = chunk.get("section")
        text = chunk.get("text") or ""
        if kind == "html":
            text = _visible(text)
            section = _visible(section) if section else None
        key = (page, section)
        if key in groups:
            groups[key]["text"] = _merge_overlap(groups[key]["text"], text)
        else:
            heading = f"Slide {page}" if kind == "pptx" else f"Page {page}" if kind == "pdf" else section or "Text"
            if kind == "pptx" and section:
                heading += f" · {section}"
            groups[key] = {"page": page, "section": section, "heading": heading, "text": text,
                           "locator": {"page": page, "section": section}}
    return list(groups.values())


def load_document(doc_id):
    document, chunks = _record(doc_id)
    contents = _original(document)
    notice = None if contents is not None else "The original file is unavailable. Showing indexed text."
    if not chunks and contents is not None and document["kind"] not in {"pdf", "image"}:
        extractor = {"docx": extract_docx, "pptx": extract_pptx, "html": extract_html}.get(document["kind"], extract_text)
        try:
            chunks = extractor(contents)
        except Exception:
            notice = "The original could not be previewed. Showing indexed text."
    parts = _parts(chunks, document["kind"])
    page_count = max((part["page"] for part in parts), default=1)
    if document["kind"] == "pdf" and contents is not None:
        try:
            page_count = max(1, len(PdfReader(BytesIO(contents)).pages))
        except Exception:
            notice = "The PDF could not be previewed. Showing indexed text."
    return {"document": document, "parts": parts, "page_count": page_count,
            "original_available": contents is not None, "notice": notice}


def normalize_locator(data, locator=None):
    if isinstance(locator, str):
        match = re.fullmatch(r"(?:page|slide)\s+(\d+)", locator.strip(), re.I)
        section_matches = any(part["section"] == locator.strip() for part in data["parts"])
        if match:
            locator = {"page": int(match.group(1))}
        elif locator.strip().isdigit() and not section_matches:
            locator = {"page": int(locator.strip())}
        else:
            locator = {"section": locator.strip()}
    elif type(locator) is int:
        locator = {"page": locator}
    locator = locator if isinstance(locator, dict) else {}
    page = locator.get("page", locator.get("slide", 1))
    page = page if type(page) is int else 1
    page = max(1, min(page, data["page_count"]))
    section = locator.get("section")
    if isinstance(section, str) and data["document"]["kind"] == "html":
        section = _visible(section)
    if isinstance(section, str):
        matches = [part for part in data["parts"] if part["section"] == section]
        if matches:
            chosen = next((part for part in matches if part["page"] == page), matches[0])
            return dict(chosen["locator"])
    if data["document"]["kind"] not in {"pdf", "image"}:
        chosen = next((part for part in data["parts"] if part["page"] == page), None)
        if chosen is not None:
            return dict(chosen["locator"])
    return {"page": page, "section": None}


def selected_parts(data, locator):
    locator = normalize_locator(data, locator)
    kind = data["document"]["kind"]
    if kind == "image":
        return data["parts"]
    if kind in {"pdf", "pptx"}:
        return [part for part in data["parts"] if part["page"] == locator["page"]]
    return [part for part in data["parts"] if part["locator"] == locator]


def selected_part_index(data, locator):
    locator = normalize_locator(data, locator)
    return next((index for index, part in enumerate(data["parts"]) if part["locator"] == locator), 0)


@st.cache_data(show_spinner=False, max_entries=128)
@perf.operation("pypdfium2.render_and_encode")
def _cached_pdf_page(workspace, doc_id, page, version, _contents):
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(_contents)
    try:
        count = len(document)
        if count == 0:
            raise ValueError("This PDF has no pages.")
        number = max(1, min(page, count))
        pdf_page = document[number - 1]
        try:
            bitmap = pdf_page.render(scale=1.6)
            try:
                with bitmap.to_pil() as image:
                    output = BytesIO()
                    image.save(output, format="PNG")
                return {"png": output.getvalue(), "page": number, "page_count": count}
            finally:
                bitmap.close()
        finally:
            pdf_page.close()
    finally:
        document.close()


def render_pdf_page(doc_id, page):
    document, _ = _record(doc_id)
    if document["kind"] != "pdf" or type(page) is not int:
        raise ValueError("Choose a valid PDF page.")
    contents = _original(document)
    if contents is None:
        return None
    version = hashlib.sha256(contents).hexdigest()
    return _cached_pdf_page(str(get_storage().root), doc_id, page, version, contents)


def image_original(doc_id):
    from PIL import Image

    document, _ = _record(doc_id)
    if document["kind"] != "image":
        return None
    contents = _original(document)
    if contents is not None:
        try:
            with Image.open(BytesIO(contents)) as image:
                image.verify()
            return contents
        except Exception:
            return None
    return None


def request_viewer(state, doc_id, locator=None):
    data = load_document(doc_id)
    remembered = state.setdefault("viewer_locators", {})
    if locator is None:
        locator = remembered.get(doc_id)
    locator = normalize_locator(data, locator)
    remembered[doc_id] = locator
    revision = state.get("viewer_revision", 0) + 1
    state["viewer_revision"] = revision
    state["viewer_request"] = {"doc_id": doc_id, "locator": locator, "revision": revision,
                               "workspace": str(get_storage().root)}
    state.pop("diagram_view", None)


def open_viewer(doc_id, locator=None):
    request_viewer(st.session_state, doc_id, locator)


def active_viewer(state):
    request = state.get("viewer_request")
    if request and request["workspace"] != str(get_storage().root):
        close_viewer(state)
        return None
    return request


def close_viewer(state):
    state.pop("viewer_request", None)


def choose_page(state, widget_key):
    request = active_viewer(state)
    if request:
        request_viewer(state, request["doc_id"], {"page": state.get(widget_key, 1)})


def move_page(state, offset):
    request = active_viewer(state)
    if request:
        request_viewer(state, request["doc_id"], {"page": request["locator"]["page"] + offset})


def choose_section(state, widget_key):
    request = active_viewer(state)
    if request:
        data = load_document(request["doc_id"])
        index = state.get(widget_key)
        if type(index) is int and 0 <= index < len(data["parts"]):
            request_viewer(state, request["doc_id"], data["parts"][index]["locator"])


def add_current_bookmark(state, label_key):
    from .notes import add_bookmark

    request = active_viewer(state)
    if request is None:
        raise ValueError("Open a document before adding a bookmark.")
    add_bookmark(request["doc_id"], request["locator"], state.get(label_key, ""))
    state[label_key] = ""
    state["viewer_notice"] = "Bookmark saved."
