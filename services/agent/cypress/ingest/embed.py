"""Vertex AI embeddings (catalog embedding model, 768 dims, L2-normalized, metered)."""

import math
from collections.abc import Callable

EmbedFn = Callable[[list[str]], list[list[float]]]
DIMS = 768
BATCH = 50


def normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def vertex_embedder(project: str | None = None) -> EmbedFn:
    """Document embedder using the catalog's embedding model; every batch is metered (finops)."""
    from .. import models

    def run(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            vecs = models.embed_texts(texts[i:i + BATCH], node="embed_ingest", task="RETRIEVAL_DOCUMENT", dims=DIMS)
            # Truncated (non-3072) gemini embeddings must be normalized for cosine distance.
            out += [normalize(v) for v in vecs]
        return out

    return run
