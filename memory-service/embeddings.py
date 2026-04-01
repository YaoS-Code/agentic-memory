"""Embedding pipeline using BAAI/bge-m3."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

logger = logging.getLogger(__name__)

_model = None


def get_model():
    """Lazy-load the embedding model (runs once at first call)."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        logger.info("Loading embedding model BAAI/bge-m3 ...")
        _model = SentenceTransformer("BAAI/bge-m3")
        logger.info("Embedding model loaded, dim=%d", _model.get_sentence_embedding_dimension())
    return _model


def embed_texts(texts: list[str], is_query: bool = False) -> list[list[float]]:
    """Generate embeddings for a list of texts.

    Args:
        texts: List of strings to embed.
        is_query: If True, prepend query instruction for asymmetric search.

    Returns:
        List of embedding vectors (each 1024-dim).
    """
    if not texts:
        return []

    model = get_model()

    # bge-m3 doesn't require special prefixes like nomic, but
    # for retrieval tasks, shorter queries benefit from instruction
    if is_query:
        prompt_name = "query"
    else:
        prompt_name = None

    embeddings = model.encode(
        texts,
        normalize_embeddings=True,
        batch_size=32,
        show_progress_bar=False,
    )

    return embeddings.tolist()


def embed_single(text: str, is_query: bool = False) -> list[float]:
    """Embed a single text."""
    results = embed_texts([text], is_query=is_query)
    return results[0]


def preload():
    """Preload model at startup."""
    get_model()
