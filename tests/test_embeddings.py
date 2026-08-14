"""Tests for the Chroma embedding pipeline.

Design rules (from CLAUDE.md):
- State the expected count BEFORE checking it.
- Assert on exact counts, not just "it ran".
- The idempotency test (two consecutive runs produce the same count) is
  the highest-value test here — it catches silent duplication.
"""

import sys
from pathlib import Path

import pytest

# Make sure the repo root is on the path (mirrors conftest.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embeddings.embed import (
    COLLECTION_NAME,
    _chunk_id,
    _iter_chunks,
    embed_repository,
    get_collection,
    query,
)
from chromadb import EmbeddingFunction, Documents, Embeddings


# ---------------------------------------------------------------------------
# Offline-safe stub embedding function.
#
# Chroma 1.5+ requires the embedding function to be an EmbeddingFunction
# subclass with a .name() method.  384 dimensions matches all-MiniLM-L6-v2
# so collections created in tests are dimensionally consistent, but the
# values are irrelevant for the correctness properties being tested here.
# ---------------------------------------------------------------------------

class _StubEF(EmbeddingFunction):
    """Deterministic fake embeddings — no network required."""

    def __init__(self) -> None:
        pass

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002
        return [[float(i % 10) / 10.0] * 384 for i, _ in enumerate(input)]

    def name(self) -> str:
        return "stub-ef"

    def get_config(self) -> dict:
        return {"name": self.name()}


_stub_ef = _StubEF()


# ---------------------------------------------------------------------------
# Minimal synthetic repository used across all tests
# ---------------------------------------------------------------------------

_MINI_SOURCE = '''\
def alpha(x):
    return x + 1


class Beta:

    def gamma(self):
        return 0
'''

# Expected chunk counts for _MINI_SOURCE:
#   1 file chunk  +  1 top-level function (alpha)  +  1 method (Beta.gamma)
_EXPECTED_CHUNK_COUNT = 3


@pytest.fixture()
def mini_repo(tmp_path: Path) -> Path:
    """Write a two-file synthetic repository under tmp_path."""
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "mod_a.py").write_text(_MINI_SOURCE, encoding="utf-8")
    (pkg / "mod_b.py").write_text("def delta():\n    pass\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def chroma_dir(tmp_path: Path) -> str:
    """Isolated Chroma storage directory for each test."""
    return str(tmp_path / ".chroma_test")


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_chunk_id_is_deterministic():
    """The same inputs must always produce the same ID."""
    id1 = _chunk_id("repo-a", "pkg/mod.py", "func:1-5")
    id2 = _chunk_id("repo-a", "pkg/mod.py", "func:1-5")
    assert id1 == id2


def test_chunk_id_differs_across_repos():
    """Repo isolation: same file+label in different repos must differ."""
    id_a = _chunk_id("repo-a", "pkg/mod.py", "func:1-5")
    id_b = _chunk_id("repo-b", "pkg/mod.py", "func:1-5")
    assert id_a != id_b


def test_iter_chunks_count(mini_repo: Path):
    """_iter_chunks must yield exactly the predicted number of chunks per file.

    mod_a.py: 1 file + 1 function (alpha) + 1 method (Beta.gamma) = 3
    mod_b.py: 1 file + 1 function (delta)                          = 2
    Total expected: 5
    """
    py_files = sorted(mini_repo.rglob("*.py"))
    chunks = list(_iter_chunks("testrepo", mini_repo, py_files))

    expected_total = 5  # stated before checking
    assert len(chunks) == expected_total, (
        f"Expected {expected_total} chunks, got {len(chunks)}: "
        + str([c[0] for c in chunks])
    )


def test_iter_chunks_metadata_has_repo(mini_repo: Path):
    """Every chunk must carry the repo identifier in its metadata."""
    py_files = list(mini_repo.rglob("*.py"))
    for _doc_id, _text, meta in _iter_chunks("myrepo", mini_repo, py_files):
        assert meta["repo"] == "myrepo", f"Missing/wrong repo in: {meta}"


def test_iter_chunks_metadata_has_file_and_lines(mini_repo: Path):
    """Every chunk must carry file, start_line, and end_line."""
    py_files = list(mini_repo.rglob("*.py"))
    for _doc_id, text, meta in _iter_chunks("myrepo", mini_repo, py_files):
        assert "file" in meta, f"No 'file' key: {meta}"
        assert "start_line" in meta, f"No 'start_line' key: {meta}"
        assert "end_line" in meta, f"No 'end_line' key: {meta}"


# ---------------------------------------------------------------------------
# Integration tests (write to a temporary Chroma directory)
# ---------------------------------------------------------------------------

def test_embed_repository_document_count(mini_repo: Path, chroma_dir: str):
    """embed_repository must upsert exactly the predicted number of documents.

    Mini repo: mod_a (3 chunks) + mod_b (2 chunks) = 5 total documents.
    """
    expected_docs = 5  # stated before checking

    upserted = embed_repository(
        str(mini_repo), repo_id="ri-test",
        chroma_path=chroma_dir, embedding_function=_stub_ef,
    )

    assert upserted == expected_docs, (
        f"Expected {expected_docs} upserted, got {upserted}"
    )

    # Confirm the collection actually holds that many documents for this repo.
    col = get_collection(chroma_dir, embedding_function=_stub_ef)
    stored = col.count()
    assert stored == expected_docs, (
        f"Expected {expected_docs} stored, found {stored}"
    )


def test_embed_repository_idempotent(mini_repo: Path, chroma_dir: str):
    """Re-running on unchanged source must NOT duplicate entries.

    Run 1: 5 documents.  Run 2: still 5 documents.
    """
    embed_repository(
        str(mini_repo), repo_id="ri-idem",
        chroma_path=chroma_dir, embedding_function=_stub_ef,
    )

    col = get_collection(chroma_dir, embedding_function=_stub_ef)
    count_after_first_run = col.count()

    embed_repository(
        str(mini_repo), repo_id="ri-idem",
        chroma_path=chroma_dir, embedding_function=_stub_ef,
    )
    count_after_second_run = col.count()

    expected = 5  # stated before checking
    assert count_after_first_run == expected, (
        f"After first run: expected {expected}, got {count_after_first_run}"
    )
    assert count_after_second_run == expected, (
        f"After second run (idempotency): expected {expected}, "
        f"got {count_after_second_run} (was {count_after_first_run} before)"
    )


def test_embed_two_repos_are_isolated(mini_repo: Path, chroma_dir: str):
    """Embeddings for repo A must be filterable from repo B.

    Both repos use the same source files; after embedding both, filtering
    by repo should return exactly 5 documents each, not 10.
    """
    embed_repository(
        str(mini_repo), repo_id="repo-alpha",
        chroma_path=chroma_dir, embedding_function=_stub_ef,
    )
    embed_repository(
        str(mini_repo), repo_id="repo-beta",
        chroma_path=chroma_dir, embedding_function=_stub_ef,
    )

    col = get_collection(chroma_dir, embedding_function=_stub_ef)
    total_stored = col.count()

    # 5 unique IDs per repo (IDs are scoped by repo_id), so total = 10.
    expected_total = 10  # stated before checking
    assert total_stored == expected_total, (
        f"Expected {expected_total} total across two repos, got {total_stored}"
    )

    # Filter to repo-alpha only; should return exactly 5.
    results_alpha = col.get(where={"repo": "repo-alpha"})
    expected_per_repo = 5
    assert len(results_alpha["ids"]) == expected_per_repo, (
        f"Expected {expected_per_repo} docs for repo-alpha, "
        f"got {len(results_alpha['ids'])}"
    )


def test_query_scoped_to_repo(mini_repo: Path, chroma_dir: str):
    """query() scoped to a repo_id must not bleed results from another repo."""
    embed_repository(
        str(mini_repo), repo_id="scope-a",
        chroma_path=chroma_dir, embedding_function=_stub_ef,
    )
    embed_repository(
        str(mini_repo), repo_id="scope-b",
        chroma_path=chroma_dir, embedding_function=_stub_ef,
    )

    results = query(
        "function that returns integer",
        repo_id="scope-a",
        n_results=3,
        chroma_path=chroma_dir,
        embedding_function=_stub_ef,
    )

    returned_repos = {m["repo"] for m in results["metadatas"][0]}
    assert returned_repos == {"scope-a"}, (
        f"Expected only scope-a results, got repos: {returned_repos}"
    )
