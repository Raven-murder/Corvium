from .config import StorageError
import math

from .db import database_session
from .extractors import extract_html
from .ingest import search as retrieve_passages
from .sources import passage_preview
from .usage import reserve_workspace_action, UsageLimitError
from .workspaces import get_storage, Storage, validate_workspace_id, WorkspaceError


class SearchResults(list):
    def __init__(self, documents=(), notice=None):
        super().__init__(documents)
        self.notice = notice


def _workspace(workspace):
    current = get_storage()
    if isinstance(workspace, Storage):
        matches = workspace == current
    else:
        matches = validate_workspace_id(workspace) == current.workspace_id
    if not matches:
        raise WorkspaceError("Search is unavailable in this workspace. Please refresh the page.")
    return current


def _passage(chunk, kind):
    section = chunk.get("section")
    page = chunk["page"]
    text = chunk["text"]
    if kind == "html":
        text = "\n".join(part["text"] for part in extract_html(text))
        section = "\n".join(part["text"] for part in extract_html(section)) if section else None
    heading = f"Slide {page}" if kind == "pptx" else f"Page {page}" if kind in {"pdf", "image"} else section or "Text"
    return {"text": passage_preview(text), "heading": heading,
            "locator": {"page": page, "section": section}}


def search_documents(query, workspace):
    """Search the current workspace, accepting its Storage or validated workspace ID."""
    _workspace(workspace)
    if not isinstance(query, str) or not query.strip():
        return SearchResults()
    query = " ".join(query.split())
    with database_session() as connection:
        documents = [dict(row) for row in connection.execute(
            "SELECT d.*, COALESCE(f.name, 'Unfiled') AS folder_name FROM documents d "
            "LEFT JOIN folders f ON f.id = d.folder_id ORDER BY d.position, d.id"
        )]
        chunks = [dict(row) for row in connection.execute(
            "SELECT doc_id, page, section, text FROM chunks ORDER BY page, id"
        )]
    semantic, notice = [], None
    try:
        reserve_workspace_action("tool")
        if chunks:
            semantic = retrieve_passages(query, k=len(chunks), embedding_attempts=1)
    except StorageError:
        raise
    except UsageLimitError:
        notice = "Semantic search is unavailable because the demo's usage limit has been reached. Filename matches are still shown."
    except Exception:
        notice = "Semantic search is temporarily unavailable. Filename matches are still shown."
    by_document = {}
    for chunk in semantic:
        if math.isfinite(chunk["score"]):
            by_document.setdefault(chunk["doc_id"], []).append(chunk)
    fallback = {}
    for chunk in chunks:
        fallback.setdefault(chunk["doc_id"], []).append(chunk)
    results = []
    for document in documents:
        name_match = any(query.casefold() in document[field].casefold() for field in ("name", "display_name"))
        passages = sorted(by_document.get(document["id"], []), key=lambda chunk: -chunk["score"])
        semantic_score = max(0.0, passages[0]["score"]) if passages else 0.0
        if not name_match and semantic_score <= 0:
            continue
        best = passages or fallback.get(document["id"], [])
        previews, seen = [], set()
        for chunk in best:
            identity = (chunk["page"], chunk.get("section"), chunk["text"])
            if identity not in seen:
                seen.add(identity)
                previews.append(_passage(chunk, document["kind"]))
            if len(previews) == 2:
                break
        results.append({**document, "passages": previews, "score": 2.0 * name_match + semantic_score,
                        "locator": previews[0]["locator"] if previews else {"page": 1, "section": None}})
    results.sort(key=lambda document: (-document["score"], document["display_name"].casefold(), document["id"]))
    return SearchResults(results, notice)


def queue_search(state):
    state["pending_document_search"] = state.get("document_search_query", "")


def run_pending_search(state):
    if "pending_document_search" not in state:
        return
    query = state.pop("pending_document_search")
    workspace = get_storage()
    results = search_documents(query, workspace)
    state["document_search_results"] = {"documents": list(results), "notice": results.notice,
                                        "workspace": str(workspace.root), "query": query.strip()}


def search_results(state):
    results = state.get("document_search_results")
    if results and results["workspace"] != str(get_storage().root):
        state.pop("document_search_results", None)
        return None
    return results
