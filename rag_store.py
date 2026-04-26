"""
SonarAI — RAG Store  (Iteration 2)
ChromaDB-backed vector store for prior fix retrieval.

Workflow:
  1. After a successful fix, store_fix() embeds the fix context and saves it.
  2. Before planning, retrieve_similar_fixes() fetches the top-k most similar
     prior fixes by rule_key + method context embedding.
  3. The Planner prompt includes the retrieved examples as few-shot context.

Embeddings: VertexAI text-embedding-005 (768-dim) via langchain-google-vertexai.
Storage: ChromaDB persisted to disk at settings.chroma_persist_dir.

Graceful degradation: if ChromaDB or Vertex embeddings are unavailable,
all public functions return empty results and log a warning — the pipeline
continues without RAG context.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# Lazy imports so missing chromadb doesn't crash the entire pipeline
_chroma_client = None
_collection = None
_embed_fn = None

COLLECTION_NAME = "sonar_ai_fixes"
TOP_K = 3


def _get_embed_fn():
    """Return a VertexAIEmbeddings callable, cached."""
    global _embed_fn
    if _embed_fn is not None:
        return _embed_fn
    try:
        from langchain_google_vertexai import VertexAIEmbeddings
        from config import settings
        _embed_fn = VertexAIEmbeddings(
            model_name=settings.embedding_model,
            project=settings.gcp_project,
            location=settings.gcp_location,
        )
        logger.info(f"[RAG] Embeddings initialised: {settings.embedding_model}")
    except Exception as exc:
        logger.warning(f"[RAG] Could not initialise VertexAI embeddings: {exc}")
        _embed_fn = None
    return _embed_fn


def _get_collection():
    """Return (or create) the ChromaDB collection, cached."""
    global _chroma_client, _collection
    if _collection is not None:
        return _collection
    try:
        import chromadb
        from config import settings
        persist_dir = settings.chroma_persist_dir
        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=persist_dir)
        _collection = _chroma_client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            f"[RAG] ChromaDB collection '{COLLECTION_NAME}' ready "
            f"({_collection.count()} documents) at {persist_dir}"
        )
    except Exception as exc:
        logger.warning(f"[RAG] ChromaDB unavailable: {exc}")
        _collection = None
    return _collection


def _embed(text: str) -> Optional[list[float]]:
    """Embed text using VertexAI; return None on any failure."""
    fn = _get_embed_fn()
    if fn is None:
        return None
    try:
        result = fn.embed_query(text)
        return result
    except Exception as exc:
        logger.warning(f"[RAG] Embedding failed: {exc}")
        return None


def _make_doc_id(rule_key: str, patch_hunks: str) -> str:
    """Stable SHA-based ID for a fix document."""
    content = f"{rule_key}:{patch_hunks}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _build_embed_text(rule_key: str, method_context: str, message: str) -> str:
    """Concatenate the fields most useful for similarity search."""
    return f"Rule: {rule_key}\nMessage: {message}\n\n{method_context[:1500]}"


# ── Public API ─────────────────────────────────────────────────────────────────

def retrieve_similar_fixes(
    rule_key: str,
    method_context: str,
    message: str,
    top_k: int = TOP_K,
) -> list[dict[str, Any]]:
    """
    Return the top-k most similar prior fixes from ChromaDB.

    Each result dict has:
      patch_hunks  : str
      reasoning    : str
      confidence   : float
      file_name    : str
      rule_key     : str
      similarity   : float  (1 - cosine distance)

    Returns [] on any error or if ChromaDB / embeddings are unavailable.
    """
    collection = _get_collection()
    if collection is None:
        return []

    embed_text = _build_embed_text(rule_key, method_context, message)
    embedding = _embed(embed_text)
    if embedding is None:
        return []

    try:
        # Filter by rule_key to prefer same-rule examples; fall back to all rules
        where_filter = {"rule_key": {"$eq": rule_key}}
        try:
            result = collection.query(
                query_embeddings=[embedding],
                n_results=min(top_k, collection.count()),
                where=where_filter,
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            # Fewer docs than requested or no matching rule — retry without filter
            result = collection.query(
                query_embeddings=[embedding],
                n_results=min(top_k, max(1, collection.count())),
                include=["documents", "metadatas", "distances"],
            )

        fixes: list[dict[str, Any]] = []
        docs = result.get("documents", [[]])[0]
        metas = result.get("metadatas", [[]])[0]
        dists = result.get("distances", [[]])[0]

        for doc, meta, dist in zip(docs, metas, dists):
            similarity = max(0.0, 1.0 - dist)  # cosine: dist=0 → similarity=1
            if similarity < 0.3:
                # Too dissimilar — not useful as a few-shot example
                continue
            fixes.append({
                "patch_hunks": meta.get("patch_hunks", ""),
                "reasoning": meta.get("reasoning", ""),
                "confidence": float(meta.get("confidence", 0.5)),
                "file_name": meta.get("file_name", ""),
                "rule_key": meta.get("rule_key", rule_key),
                "similarity": round(similarity, 3),
            })

        logger.info(
            f"[RAG] Retrieved {len(fixes)} similar fix(es) for rule={rule_key} "
            f"(top similarity={fixes[0]['similarity'] if fixes else 'N/A'})"
        )
        return fixes

    except Exception as exc:
        logger.warning(f"[RAG] Query failed: {exc}")
        return []


def store_fix(
    rule_key: str,
    method_context: str,
    message: str,
    patch_hunks: str,
    reasoning: str,
    confidence: float,
    file_name: str,
) -> bool:
    """
    Embed and persist a successful fix to ChromaDB.

    Returns True on success, False on any error.
    Silently skips if ChromaDB or embeddings are unavailable.
    """
    collection = _get_collection()
    if collection is None:
        return False

    embed_text = _build_embed_text(rule_key, method_context, message)
    embedding = _embed(embed_text)
    if embedding is None:
        return False

    doc_id = _make_doc_id(rule_key, patch_hunks)

    # Truncate patch_hunks for metadata storage (ChromaDB has a 512-byte metadata limit per value)
    patch_preview = patch_hunks[:400] if patch_hunks else ""
    reasoning_preview = reasoning[:300] if reasoning else ""

    try:
        collection.upsert(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[embed_text],
            metadatas=[{
                "rule_key": rule_key,
                "patch_hunks": patch_preview,
                "reasoning": reasoning_preview,
                "confidence": str(confidence),
                "file_name": file_name,
                "message": message[:200],
            }],
        )
        logger.info(
            f"[RAG] Stored fix for rule={rule_key} file={file_name} "
            f"(id={doc_id}, total_docs={collection.count()})"
        )
        return True
    except Exception as exc:
        logger.warning(f"[RAG] Failed to store fix: {exc}")
        return False


def collection_stats() -> dict[str, Any]:
    """Return basic stats about the ChromaDB collection."""
    collection = _get_collection()
    if collection is None:
        return {"available": False, "count": 0}
    try:
        return {"available": True, "count": collection.count(), "name": COLLECTION_NAME}
    except Exception:
        return {"available": False, "count": 0}
