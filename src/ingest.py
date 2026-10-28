"""Ingest notes and retrieve source passages."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
from io import BytesIO
import tempfile
import sys
import time

import numpy as np
from pypdf import PdfReader

if __package__:
    from .config import EMBED_MODEL, PROJECT_ROOT, get_client, get_vision_model, safe_error
    from .db import connect, embedding_from_blob, embedding_to_blob, init_db, infer_document_kind, normalize_document_ids
    from .extractors import extract_docx, extract_pptx, extract_html, extract_text
    from .ocr import ocr_image
    from .workspaces import get_storage, WorkspaceError
    from .usage import reserve_workspace_action, UsageLimitError
else:
    from config import EMBED_MODEL, PROJECT_ROOT, get_client, get_vision_model, safe_error
    from db import connect, embedding_from_blob, embedding_to_blob, init_db, infer_document_kind, normalize_document_ids
    from extractors import extract_docx, extract_pptx, extract_html, extract_text
    from ocr import ocr_image
    from workspaces import get_storage, WorkspaceError
    from usage import reserve_workspace_action, UsageLimitError

VECTOR_LENGTH = 4096
MAX_FILE_BYTES = 25 * 1024 * 1024


def _read_file(path):
    path = Path(path)
    storage = get_storage()
    if storage.workspace_id is not None and not path.resolve().is_relative_to(storage.root):
        raise WorkspaceError("This file is not available in the current workspace.")
    if not path.is_file():
        raise ValueError(f"File does not exist: {path.name}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("This file exceeds the 25 MB limit. Please upload a smaller file.")
    with path.open("rb") as source:
        contents = source.read(MAX_FILE_BYTES + 1)
    if len(contents) > MAX_FILE_BYTES:
        raise ValueError("This file exceeds the 25 MB limit. Please upload a smaller file.")
    return contents


def _render_pdf_page(contents, index):
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(contents)
    try:
        page = document[index]
        try:
            bitmap = page.render(scale=2)
            try:
                with bitmap.to_pil() as image:
                    output = BytesIO()
                    image.save(output, format="PNG")
                return output.getvalue()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()


def _extract_contents(contents, suffix):
    if suffix == ".pdf":
        pages = []
        for number, page in enumerate(PdfReader(BytesIO(contents)).pages, 1):
            text = (page.extract_text() or "").strip()
            warnings = []
            if len(text) < 30:
                if get_vision_model():
                    text = ocr_image(_render_pdf_page(contents, number - 1)) or text
                else:
                    warnings.append("OCR not configured")
            pages.append({"page": number, "text": text, "section": None, "warnings": warnings})
        return pages
    if suffix == ".docx":
        return extract_docx(contents)
    if suffix == ".pptx":
        return extract_pptx(contents)
    if suffix in {".html", ".htm"}:
        return extract_html(contents)
    if infer_document_kind("file" + suffix) == "image":
        configured = bool(get_vision_model())
        return [{
            "page": 1, "section": None,
            "text": ocr_image(contents) if configured else "",
            "warnings": [] if configured else ["OCR not configured"],
        }]
    return extract_text(contents)


def extract_pages(path):
    return _extract_contents(_read_file(path), Path(path).suffix.lower())


def chunk_text(text, page=1, chunk_size=2500, overlap=300, section=None):
    """Split one page into overlapping character windows; preserve page metadata."""
    if chunk_size <= 0 or not 0 <= overlap < chunk_size:
        raise ValueError("Chunk size must be positive; overlap must be smaller.")
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        passage = text[start:end].strip()
        if passage:
            chunks.append({"page": page, "text": passage, "section": section})
        if end == len(text):
            break
        start = end - overlap
    return chunks


def embed_texts(texts: list[str]):
    """Embed in batches of 32 with three attempts per batch."""
    if not texts:
        return np.empty((0, VECTOR_LENGTH), dtype=np.float32)
    client = get_client()
    vectors = []
    for start in range(0, len(texts), 32):
        batch = texts[start:start + 32]
        for attempt in range(3):
            try:
                response = client.embeddings.create(model=EMBED_MODEL, input=batch)
                items = sorted(response.data, key=lambda item: item.index)
                if [item.index for item in items] != list(range(len(batch))):
                    raise ValueError("Embedding response has missing or duplicate indices.")
                array = np.asarray([item.embedding for item in items], dtype=np.float32)
                if array.shape != (len(batch), VECTOR_LENGTH):
                    raise ValueError(f"Unexpected embedding shape: {array.shape}")
                if not np.isfinite(array).all() or np.any(np.linalg.norm(array, axis=1) == 0):
                    raise ValueError("Embedding response contains invalid or zero vectors.")
                vectors.append(array)
                break
            except UsageLimitError:
                raise
            except Exception as error:
                if attempt == 2:
                    raise RuntimeError(
                        f"Embedding batch {start // 32 + 1} failed after 3 attempts: "
                        f"{safe_error(error)}"
                    ) from None
                time.sleep(2 ** attempt)
    return np.concatenate(vectors, axis=0)


def ingest_file(path):
    """Replace indexed content while preserving document identity and library metadata."""
    path = Path(path)
    contents = _read_file(path)
    reserve_workspace_action("upload")
    pages = _extract_contents(contents, path.suffix.lower())
    chunks = [
        chunk for page in pages
        for chunk in chunk_text(page["text"], page=page["page"], section=page.get("section"))
    ]
    warnings = list(dict.fromkeys(warning for page in pages for warning in page.get("warnings", [])))
    status = "; ".join((["indexed"] if chunks else ["no text found"]) + warnings)
    vectors = embed_texts([chunk["text"] for chunk in chunks]) if chunks else []
    init_db()
    files_dir = get_storage().files_dir
    files_dir.mkdir(parents=True, exist_ok=True)
    staged = None
    destination = None
    previous = None
    replaced = False
    connection = connect()
    try:
        with tempfile.NamedTemporaryFile(dir=files_dir, prefix="ingest-", delete=False) as temporary:
            staged = Path(temporary.name)
            temporary.write(contents)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT id FROM documents WHERE name = ?", (path.name,)).fetchone()
            uploaded_at = datetime.now(timezone.utc).isoformat()
            kind = infer_document_kind(path.name)
            if existing:
                document_id = existing["id"]
                connection.execute("DELETE FROM chunks WHERE doc_id = ?", (document_id,))
            else:
                document_id = connection.execute(
                    "INSERT INTO documents(name, uploaded_at, n_chunks, display_name, kind) VALUES (?, ?, ?, ?, ?)",
                    (path.name, uploaded_at, len(chunks), path.name, kind),
                ).lastrowid
            destination = files_dir / f"{document_id}{path.suffix.lower() or '.txt'}"
            if destination.resolve() != destination.absolute():
                raise WorkspaceError("This file is not available in the current workspace.")
            previous = destination.read_bytes() if destination.exists() else None
            connection.execute(
                "UPDATE documents SET uploaded_at = ?, n_chunks = ?, kind = ?, stored_path = ?, status = ? WHERE id = ?",
                (uploaded_at, len(chunks), kind, str(destination.relative_to(get_storage().root)), status, document_id),
            )
            connection.executemany(
                "INSERT INTO chunks(doc_id, page, text, embedding, section) VALUES (?, ?, ?, ?, ?)",
                [(document_id, chunk["page"], chunk["text"], embedding_to_blob(vector), chunk["section"])
                 for chunk, vector in zip(chunks, vectors)],
            )
            staged.replace(destination)
            replaced = True
    except Exception:
        if replaced:
            if previous is None:
                destination.unlink(missing_ok=True)
            else:
                destination.write_bytes(previous)
        raise
    finally:
        connection.close()
        if staged is not None:
            staged.unlink(missing_ok=True)
    return len(chunks)


def get_ingest_status(name):
    connection = connect()
    try:
        row = connection.execute("SELECT status FROM documents WHERE name = ?", (name,)).fetchone()
        return row["status"] if row is not None else "not indexed"
    finally:
        connection.close()


def ingest_files(paths):
    results = []
    for path in paths:
        name = Path(path).name
        try:
            count = ingest_file(path)
            results.append({"name": name, "count": count, "status": get_ingest_status(name), "error": None})
        except Exception as error:
            results.append({"name": name, "count": 0, "status": "failed", "error": safe_error(error)})
    return results


def search(query, k=5, source_doc_ids=None):
    """Return top-k source passages ranked by brute-force cosine similarity."""
    if not query.strip():
        raise ValueError("Search query must not be empty.")
    if k <= 0:
        raise ValueError("k must be positive.")
    ids = normalize_document_ids(source_doc_ids)
    if ids == []:
        return []
    init_db()
    connection = connect()
    try:
        where = " WHERE documents.id IN (" + ",".join("?" for _ in ids) + ")" if ids is not None else ""
        rows = connection.execute(
            "SELECT documents.id AS doc_id, documents.display_name AS doc_name, chunks.page, chunks.section, chunks.text, chunks.embedding "
            "FROM chunks JOIN documents ON documents.id = chunks.doc_id" + where + " ORDER BY chunks.id",
            ids or [],
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        return []
    vectors = np.stack([embedding_from_blob(row["embedding"]) for row in rows])
    if vectors.shape[1] != VECTOR_LENGTH or not np.isfinite(vectors).all():
        raise ValueError("Stored embeddings are invalid; re-ingest the documents.")
    query_vector = embed_texts([query])[0]
    norms = np.linalg.norm(vectors, axis=1)
    scores = np.divide(
        vectors @ query_vector,
        norms * np.linalg.norm(query_vector),
        out=np.full(len(rows), -np.inf, dtype=np.float32),
        where=norms > 0,
    )
    order = np.argsort(-scores, kind="stable")
    return [
        {"doc_id": rows[index]["doc_id"], "doc_name": rows[index]["doc_name"],
         "section": rows[index]["section"], "page": rows[index]["page"],
         "text": rows[index]["text"], "score": float(scores[index])}
        for index in order[np.isfinite(scores[order])][:k]
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("add").add_argument("path")
    commands.add_parser("search").add_argument("question")
    args = parser.parse_args()
    try:
        if args.command == "add":
            count = ingest_file(args.path)
            print(f"Added {Path(args.path).name}: {count} chunks; {get_ingest_status(Path(args.path).name)}.")
        else:
            hits = search(args.question)
            if not hits:
                print("No indexed chunks. Add a document first.")
            for hit in hits:
                print(f"{hit['doc_name']} | page {hit['page']} | score {hit['score']:.4f}")
                print(hit["text"][:200].replace("\n", " "))
        return 0
    except Exception as error:
        print(f"Error: {safe_error(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
