"""Ingest PDF/text/Markdown documents and retrieve source passages.

From D:\\Project Zero with the virtual environment active:
    python src/ingest.py add "README.md"
    python src/ingest.py search "What is Project Zero?"

Use an existing nonempty document for add. Success prints 'Added <name>: N chunks.';
search prints ranked document/page/score lines and passage previews, or 'No indexed
chunks. Add a document first.' An empty README reports a readable error instead.
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

import numpy as np
from pypdf import PdfReader

if __package__:
    from .config import EMBED_MODEL, get_client, safe_error
    from .db import connect, embedding_from_blob, embedding_to_blob, init_db
else:
    from config import EMBED_MODEL, get_client, safe_error
    from db import connect, embedding_from_blob, embedding_to_blob, init_db

VECTOR_LENGTH = 4096


def extract_pages(path):
    """Return nonempty {page, text} entries, using one-based PDF page numbers."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"File does not exist: {path}")
    if path.suffix.lower() == ".pdf":
        pages = [
            {"page": number, "text": (page.extract_text() or "").strip()}
            for number, page in enumerate(PdfReader(path).pages, start=1)
        ]
    elif path.suffix.lower() in {".txt", ".md"}:
        pages = [{"page": 1, "text": path.read_text(encoding="utf-8-sig").strip()}]
    else:
        raise ValueError("Supported document formats: .pdf, .txt, .md")
    return [page for page in pages if page["text"]]


def chunk_text(text, page=1, chunk_size=2500, overlap=300):
    """Split one page into overlapping character windows; preserve page metadata."""
    if chunk_size <= 0 or not 0 <= overlap < chunk_size:
        raise ValueError("Chunk size must be positive; overlap must be smaller.")
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        passage = text[start:end].strip()
        if passage:
            chunks.append({"page": page, "text": passage})
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
            except Exception as error:
                if attempt == 2:
                    raise RuntimeError(
                        f"Embedding batch {start // 32 + 1} failed after 3 attempts: "
                        f"{safe_error(error)}"
                    ) from None
                time.sleep(2 ** attempt)
    return np.concatenate(vectors, axis=0)


def ingest_file(path):
    """Atomically replace a same-name document after successful embedding."""
    path = Path(path)
    chunks = [
        chunk
        for page in extract_pages(path)
        for chunk in chunk_text(page["text"], page=page["page"])
    ]
    if not chunks:
        raise ValueError("No extractable text found; scanned PDFs may require OCR.")
    vectors = embed_texts([chunk["text"] for chunk in chunks])
    init_db()
    connection = connect()
    try:
        with connection:
            connection.execute("DELETE FROM documents WHERE name = ?", (path.name,))
            cursor = connection.execute(
                "INSERT INTO documents(name, uploaded_at, n_chunks) VALUES (?, ?, ?)",
                (path.name, datetime.now(timezone.utc).isoformat(), len(chunks)),
            )
            connection.executemany(
                "INSERT INTO chunks(doc_id, page, text, embedding) VALUES (?, ?, ?, ?)",
                [
                    (cursor.lastrowid, chunk["page"], chunk["text"], embedding_to_blob(vector))
                    for chunk, vector in zip(chunks, vectors)
                ],
            )
    finally:
        connection.close()
    return len(chunks)


def search(query, k=5):
    """Return top-k source passages ranked by brute-force cosine similarity."""
    if not query.strip():
        raise ValueError("Search query must not be empty.")
    if k <= 0:
        raise ValueError("k must be positive.")
    init_db()
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT documents.name AS doc_name, chunks.page, chunks.text, chunks.embedding "
            "FROM chunks JOIN documents ON documents.id = chunks.doc_id ORDER BY chunks.id"
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
        {"doc_name": rows[index]["doc_name"], "page": rows[index]["page"],
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
            print(f"Added {Path(args.path).name}: {count} chunks.")
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
