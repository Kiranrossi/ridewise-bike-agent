"""Hybrid policy search: dense (bge-small) + sparse (BM25), fused with Reciprocal Rank Fusion."""
from __future__ import annotations

import atexit
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient, models

from ridewise.config import settings

COLLECTION = "policies"
PREFETCH = 20


@lru_cache(maxsize=1)
def get_client() -> QdrantClient:
    """One client per process. Qdrant local mode locks the index folder to a single process, so if
    another process (a second app instance, a test run) holds it, open a private read copy instead."""
    settings.index_path.mkdir(parents=True, exist_ok=True)
    try:
        client = QdrantClient(path=str(settings.index_path))
    except Exception as e:                      # portalocker / RuntimeError: already accessed
        if "already accessed" not in str(e) and "lock" not in str(e).lower():
            raise
        copy = Path(tempfile.mkdtemp(prefix="ridewise-index-"))
        shutil.copytree(settings.index_path, copy, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".lock"))
        client = QdrantClient(path=str(copy))
        atexit.register(shutil.rmtree, copy, True)
    atexit.register(client.close)
    return client


@lru_cache(maxsize=1)
def dense_model() -> TextEmbedding:
    return TextEmbedding(settings.embedding_model)


@lru_cache(maxsize=1)
def sparse_model() -> SparseTextEmbedding:
    return SparseTextEmbedding("Qdrant/bm25")


def search_policy(query: str, k: int = 5, doc_ids: list[str] | None = None) -> list[dict]:
    """Top-k policy chunks for a question, each with its citation (source_label)."""
    from ridewise.ingest import ensure_index

    if not query or not query.strip():
        return []
    ensure_index()
    dense = next(dense_model().query_embed(query))
    sparse = next(sparse_model().query_embed(query))
    flt = (models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchAny(any=doc_ids))])
           if doc_ids else None)
    result = get_client().query_points(
        COLLECTION,
        prefetch=[
            models.Prefetch(query=dense.tolist(), using="dense", limit=PREFETCH, filter=flt),
            models.Prefetch(query=models.SparseVector(indices=sparse.indices.tolist(),
                                                      values=sparse.values.tolist()),
                            using="bm25", limit=PREFETCH, filter=flt),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=k,
        with_payload=True,
    )
    return [{"source": p.payload["source_label"], "doc_id": p.payload["doc_id"],
             "version": p.payload["version"], "applies_to": p.payload["applies_to"],
             "section": p.payload["section"], "page": p.payload["page"],
             "text": p.payload["text"], "score": round(p.score, 4)} for p in result.points]
