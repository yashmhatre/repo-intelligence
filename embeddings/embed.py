"""Chroma-backed embedding pipeline for semantic code search.

Embeds function-level and file-level chunks extracted from Python source and
stores them in a persistent local Chroma collection.  Each document is keyed
by a deterministic ID so re-running on an unchanged repository does NOT
duplicate entries.

The collection holds embeddings for many repositories; every document carries
a ``repo`` metadata field so results can be filtered per repository.

Usage::

    from embeddings.embed import embed_repository
    embed_repository("/path/to/repo", repo_id="myrepo")

    # Semantic search scoped to one repo:
    from embeddings.embed import get_collection, DEFAULT_CHROMA_PATH
    results = get_collection().query(
        query_texts=["authenticate user"],
        n_results=5,
        where={"repo": "myrepo"},
    )

The ``embedding_function`` parameter accepted by :func:`get_collection`,
:func:`embed_repository`, and :func:`query` lets callers substitute any
Chroma-compatible callable.  Pass a lightweight stub in tests to avoid
downloading the model from HuggingFace::

    def _stub_ef(texts):
        return [[0.0] * 384 for _ in texts]

    col = get_collection(chroma_path=tmp, embedding_function=_stub_ef)
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Iterable

import chromadb
from chromadb import EmbeddingFunction, Documents, Embeddings
from chromadb.utils import embedding_functions

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

# Resolved relative to the caller's working directory so the pipeline works
# from any working directory.  The .chroma/ directory is listed in .gitignore.
DEFAULT_CHROMA_PATH = str(Path(".chroma").resolve())

COLLECTION_NAME = "code_chunks"

# The model is downloaded once by sentence-transformers and cached locally.
_EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# Bring the repo root onto the path so `ingest` can be imported.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ingest.parse_code import list_python_files, parse_python_file  # noqa: E402


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def _default_embedding_function() -> EmbeddingFunction:
    """Return the default SentenceTransformer embedding function.

    Lazy-loaded so importing this module does not trigger a model download.
    """
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=_EMBEDDING_MODEL
    )


def get_client(chroma_path: str = DEFAULT_CHROMA_PATH) -> chromadb.PersistentClient:
    """Return a Chroma persistent client at *chroma_path*."""
    return chromadb.PersistentClient(path=chroma_path)


def get_collection(
    chroma_path: str = DEFAULT_CHROMA_PATH,
    collection_name: str = COLLECTION_NAME,
    embedding_function: EmbeddingFunction | None = None,
) -> chromadb.Collection:
    """Return (or create) the named Chroma collection.

    Parameters
    ----------
    chroma_path:
        Path to the Chroma persistent storage directory.
    collection_name:
        Name of the collection to open or create.
    embedding_function:
        Chroma-compatible callable ``(list[str]) -> list[list[float]]``.
        Defaults to :data:`_EMBEDDING_MODEL` via sentence-transformers.
        Pass a lightweight stub here in tests to avoid network downloads.
    """
    client = get_client(chroma_path)
    ef = embedding_function if embedding_function is not None else _default_embedding_function()
    return client.get_or_create_collection(
        name=collection_name,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )


def _chunk_id(repo: str, file_path: str, label: str) -> str:
    """Deterministic document ID based on repo + file + label.

    Using a hash keeps IDs short and safe for any label string.
    """
    raw = f"{repo}::{file_path}::{label}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _iter_chunks(
    repo: str,
    repo_root: Path,
    py_files: list[Path],
) -> Iterable[tuple[str, str, dict]]:
    """Yield (doc_id, text, metadata) for every embeddable chunk.

    Chunks:
    * One per top-level function / method (text = qualified name + source
      lines if available, else just the qualified name).
    * One per file (text = relative file path).
    """
    for py_file in py_files:
        try:
            parsed = parse_python_file(py_file)
        except Exception:
            continue

        rel_path = str(py_file.relative_to(repo_root))
        source_lines: list[str] = []
        try:
            source_lines = py_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            pass

        # --- file-level chunk ---
        file_id = _chunk_id(repo, rel_path, "__file__")
        yield (
            file_id,
            rel_path,
            {
                "repo": repo,
                "file": rel_path,
                "kind": "file",
                "start_line": 1,
                "end_line": len(source_lines) or 0,
            },
        )

        # --- function-level chunks ---
        def _fn_chunks(functions: list[dict], prefix: str = "") -> None:
            for fn in functions:
                qname = f"{prefix}{fn['qualname']}" if prefix else fn["qualname"]
                start = fn.get("start_line", fn["line"])
                end = fn.get("end_line", fn["line"])

                if source_lines and start > 0 and end <= len(source_lines):
                    snippet = "\n".join(source_lines[start - 1 : end])
                else:
                    snippet = qname

                label = f"{qname}:{start}-{end}"
                fn_id = _chunk_id(repo, rel_path, label)
                yield (
                    fn_id,
                    snippet,
                    {
                        "repo": repo,
                        "file": rel_path,
                        "kind": "function",
                        "name": qname,
                        "start_line": start,
                        "end_line": end,
                    },
                )

        yield from _fn_chunks(parsed.get("functions", []))

        for cls in parsed.get("classes", []):
            yield from _fn_chunks(cls.get("methods", []))


def embed_repository(
    repo_path: str,
    repo_id: str,
    chroma_path: str = DEFAULT_CHROMA_PATH,
    collection_name: str = COLLECTION_NAME,
    batch_size: int = 64,
    embedding_function: EmbeddingFunction | None = None,
) -> int:
    """Embed all Python chunks in *repo_path* and upsert into Chroma.

    Parameters
    ----------
    repo_path:
        Absolute or relative path to the repository root.
    repo_id:
        Short identifier stored as ``repo`` metadata on every document.
        Must be unique per repository so results are filterable.
    chroma_path:
        Path to the Chroma persistent storage directory.
    collection_name:
        Chroma collection to write into.
    batch_size:
        Number of documents to upsert per Chroma call.
    embedding_function:
        Chroma-compatible callable.  Defaults to :data:`_EMBEDDING_MODEL`.
        Pass a stub in tests to avoid downloading the model.

    Returns
    -------
    int
        Total number of documents upserted (not deduplicated count).
    """
    root = Path(repo_path).resolve()
    py_files = list_python_files(root)
    collection = get_collection(chroma_path, collection_name, embedding_function)

    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []
    total = 0

    for doc_id, text, meta in _iter_chunks(repo_id, root, py_files):
        ids.append(doc_id)
        documents.append(text)
        metadatas.append(meta)

        if len(ids) >= batch_size:
            collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
            total += len(ids)
            ids, documents, metadatas = [], [], []

    if ids:
        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        total += len(ids)

    return total


def query(
    query_text: str,
    repo_id: str | None = None,
    n_results: int = 5,
    chroma_path: str = DEFAULT_CHROMA_PATH,
    collection_name: str = COLLECTION_NAME,
    embedding_function: EmbeddingFunction | None = None,
) -> dict:
    """Semantic search over embedded chunks.

    Parameters
    ----------
    query_text:
        Natural-language query.
    repo_id:
        If given, restrict results to this repository.
    n_results:
        Maximum number of results to return.
    chroma_path, collection_name:
        Chroma storage location and collection.
    embedding_function:
        Chroma-compatible callable.  Must match the one used when the
        collection was populated.  Defaults to :data:`_EMBEDDING_MODEL`.

    Returns
    -------
    dict
        Raw Chroma query result (keys: ids, documents, metadatas, distances).
    """
    collection = get_collection(chroma_path, collection_name, embedding_function)
    kwargs: dict = {"query_texts": [query_text], "n_results": n_results}
    if repo_id is not None:
        kwargs["where"] = {"repo": repo_id}
    return collection.query(**kwargs)
