import hashlib
import mimetypes
from pathlib import Path
import re

from .workspaces import resolve_original_path
from .db import database_session, infer_document_kind
from .qa import cited_sources


def passage_preview(text, limit=600):
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    endings = [match.end() for match in re.finditer(r"[.!?]+[\"'”’\)\]]*(?=\s|$)", text) if match.end() <= limit]
    return text[:endings[-1]].rstrip() if endings else ""


def _passage_heading(kind, section, page):
    if kind == "pptx":
        return f"Slide {page}"
    if kind in {"pdf", "docx", "image"}:
        return f"Page {page}"
    return section or "Text"


def group_answer_sources(sources, answer):
    selected = cited_sources(sources, answer)
    if not selected:
        return []
    with database_session() as connection:
        documents = [dict(row) for row in connection.execute(
            "SELECT id, name, display_name, kind, stored_path FROM documents"
        )]
        by_id = {document["id"]: document for document in documents}
        by_name = {document["name"]: document for document in documents}
        groups, seen = {}, set()
        for source in selected:
            document = by_id.get(source.get("doc_id"))
            if document is None and source.get("doc_id") is None:
                document = by_name.get(source["doc_name"])
                if document is None:
                    matches = [item for item in documents if item["display_name"] == source["doc_name"]]
                    document = matches[0] if len(matches) == 1 else None
            identity = ("id", document["id"]) if document else ("source", source.get("doc_id"), source["doc_name"])
            if identity not in groups:
                name = document["name"] if document else source["doc_name"]
                groups[identity] = {
                    "doc_id": document["id"] if document else None,
                    "key": hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()[:16],
                    "doc_name": document["display_name"] if document else source["doc_name"],
                    "name": name,
                    "kind": document["kind"] if document else infer_document_kind(name),
                    "stored_path": document["stored_path"] if document else None,
                    "passages": [],
                }
            group = groups[identity]
            section = source.get("section")
            if section is None and document:
                row = connection.execute(
                    "SELECT section FROM chunks WHERE doc_id = ? AND page = ? AND text = ? ORDER BY id LIMIT 1",
                    (document["id"], source["page"], source["text"]),
                ).fetchone()
                section = row["section"] if row else None
            unique = (identity, source["page"], section, source["text"])
            if unique in seen:
                continue
            seen.add(unique)
            group["passages"].append({
                "page": source["page"],
                "heading": _passage_heading(group["kind"], section, source["page"]),
                "locator": {"page": source["page"], "section": section},
                "text": passage_preview(source["text"]),
            })
    for group in groups.values():
        group["passages"].sort(key=lambda passage: passage["page"])
    return list(groups.values())


def original_download(source_group):
    stored_path = source_group.get("stored_path")
    if not stored_path:
        return None
    path = resolve_original_path(stored_path)
    if path is None:
        return None
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    return {"data": data, "file_name": source_group["name"],
            "mime": mimetypes.guess_type(source_group["name"])[0] or "application/octet-stream"}


def open_sources_panel(state, sources, answer, message_key):
    state["sources_panel"] = {
        "sources": sources, "answer": answer, "message_key": message_key,
        "chat_id": state.get("selected_chat_id"),
    }


def active_sources_panel(state):
    panel = state.get("sources_panel")
    if panel and panel["chat_id"] != state.get("selected_chat_id"):
        close_sources_panel(state)
        return None
    return panel


def dock_sources_panel(state, side):
    if side not in {"left", "right", "bottom"}:
        raise ValueError("Choose left, right, or bottom for Sources.")
    state["sources_dock"] = side


def sources_panel_dock(state):
    side = state.get("sources_dock", "right")
    return side if side in {"left", "right", "bottom"} else "right"


def close_sources_panel(state):
    state.pop("sources_panel", None)
