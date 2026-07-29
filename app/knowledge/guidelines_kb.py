"""
Mortgage guideline knowledge base -- Chroma-backed vector search over
real ingested agency/investor guide text (app/knowledge/ingest.py does
the PDF -> chunk step; this module embeds + stores + searches).

Falls back to a tiny in-memory keyword-matched seed set only when no
real guideline has been ingested yet, so the scaffold and the demo API
routes still return *something* out of the box -- but `search_guidelines`
always prefers the real vector index once you've ingested content.

Embedding model: Chroma's SentenceTransformerEmbeddingFunction with
all-MiniLM-L6-v2 -- small, runs on CPU, good enough for guideline
retrieval. Swap for a larger model or a hosted embedding API if recall
quality on your actual guide text needs it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

from app.config import get_settings
from app.knowledge.ingest import GuidelineChunk, ingest_pdf

logger = logging.getLogger(__name__)

COLLECTION_NAME = "mortgage_guidelines"


@lru_cache
def _get_chroma_collection():
    """
    Lazy singleton: import chromadb and stand up a persistent collection
    only when first needed, so importing this module doesn't require
    chromadb to be installed if you're only running the non-KB parts of
    the scaffold (income calc, pipeline, etc. have no vector-store
    dependency).
    """
    import chromadb
    from chromadb.utils import embedding_functions

    settings = get_settings()
    client = chromadb.PersistentClient(path=getattr(settings, "chroma_persist_dir", "/data/chroma"))
    embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    return client.get_or_create_collection(name=COLLECTION_NAME, embedding_function=embedding_fn)


def ingest_guideline_pdf(file_bytes: bytes, source: str, loan_types: list[str]) -> int:
    """
    Full ingestion: PDF -> page-tracked text -> overlapping chunks ->
    embed -> upsert into the vector index. Returns the number of chunks
    written. Re-ingesting the same `source` overwrites its prior chunks
    (upsert by chunk_id), so updating a guide to a newer revision is
    just calling this again with the new file.
    """
    chunks = ingest_pdf(file_bytes, source=source, loan_types=loan_types)
    collection = _get_chroma_collection()

    collection.upsert(
        ids=[c.chunk_id for c in chunks],
        documents=[c.text for c in chunks],
        metadatas=[
            {
                "source": c.source,
                "page_start": c.page_start,
                "page_end": c.page_end,
                # Chroma metadata values must be scalars, not lists --
                # store as a comma-joined string and filter in Python
                # (see _matches_loan_type below).
                "loan_types": ",".join(c.loan_types),
            }
            for c in chunks
        ],
    )
    logger.info("Ingested %d chunks from '%s' (loan_types=%s)", len(chunks), source, loan_types)
    return len(chunks)


def _matches_loan_type(metadata: dict, loan_type: str | None) -> bool:
    if loan_type is None:
        return True
    stored = metadata.get("loan_types", "")
    return loan_type in stored.split(",")


def search_guidelines(query: str, loan_type: str | None = None, top_k: int = 3) -> list[dict]:
    try:
        collection = _get_chroma_collection()
    except ImportError:
        logger.warning("chromadb not installed -- falling back to seed keyword search. "
                        "Run `pip install chromadb sentence-transformers` and ingest real "
                        "guides for production-quality retrieval.")
        return _search_seed_fallback(query, loan_type, top_k)

    if collection.count() == 0:
        logger.warning("Guideline collection is empty -- no guides have been ingested yet. "
                        "Falling back to seed data. POST to /admin/guidelines/ingest to load real guides.")
        return _search_seed_fallback(query, loan_type, top_k)

    # Over-fetch since we post-filter by loan_type in Python (metadata
    # values are flat strings, not list-queryable in Chroma).
    raw = collection.query(query_texts=[query], n_results=top_k * 4)

    results = []
    for text, metadata, distance in zip(
        raw["documents"][0], raw["metadatas"][0], raw["distances"][0]
    ):
        if not _matches_loan_type(metadata, loan_type):
            continue
        results.append({
            "text": text,
            "source": f"{metadata['source']} (p. {metadata['page_start']}-{metadata['page_end']})",
            "loan_types": metadata["loan_types"].split(","),
            "relevance_distance": round(distance, 4),  # lower = more relevant
        })
        if len(results) >= top_k:
            break

    return results


# ---------------------------------------------------------------------
# Seed fallback: tiny illustrative dataset, used only until real guides
# are ingested (or if chromadb isn't installed in this environment).
# ---------------------------------------------------------------------

@dataclass
class _SeedChunk:
    text: str
    source: str
    loan_types: list[str]


_SEED_CHUNKS: list[_SeedChunk] = [
    _SeedChunk(
        text=(
            "Conventional conforming loans generally require a minimum "
            "credit score in the 620-680 range depending on LTV and "
            "product, with better pricing at higher scores. Confirm the "
            "current minimum with AUS (DU/LP) findings, since overlays "
            "vary by investor."
        ),
        source="Illustrative agency guidance (verify against current Fannie Mae Selling Guide)",
        loan_types=["conventional"],
    ),
    _SeedChunk(
        text=(
            "FHA loans allow credit scores as low as 500-580 depending on "
            "LTV (580+ for 96.5% LTV, 500-579 requires 10% down), subject "
            "to lender overlays which are often stricter than the FHA floor."
        ),
        source="Illustrative agency guidance (verify against current HUD 4000.1)",
        loan_types=["fha"],
    ),
    _SeedChunk(
        text=(
            "VA loans have no agency-set minimum credit score; the VA "
            "focuses on residual income and overall credit history, but "
            "individual lenders commonly overlay a 580-620 minimum."
        ),
        source="Illustrative agency guidance (verify against current VA Lender's Handbook)",
        loan_types=["va"],
    ),
    _SeedChunk(
        text=(
            "Self-employed borrowers generally must provide two years of "
            "personal (and business, if applicable) tax returns; income is "
            "typically averaged over the two years with non-recurring "
            "items and depreciation add-backs adjusted per the applicable "
            "cash-flow analysis worksheet."
        ),
        source="Illustrative agency guidance (verify against current Selling Guide B3-3.2)",
        loan_types=["conventional", "fha", "va"],
    ),
    _SeedChunk(
        text=(
            "Rental income used for qualifying is typically calculated at "
            "75% of gross rents (or per Schedule E net plus depreciation "
            "add-back) to account for vacancy and maintenance, unless a "
            "different factor is documented and supported."
        ),
        source="Illustrative agency guidance (verify against current Selling Guide B3-3.1)",
        loan_types=["conventional"],
    ),
]


def _search_seed_fallback(query: str, loan_type: str | None, top_k: int) -> list[dict]:
    query_terms = set(query.lower().split())
    scored = []
    for chunk in _SEED_CHUNKS:
        if loan_type and loan_type not in chunk.loan_types:
            continue
        overlap = len(query_terms & set(chunk.text.lower().split()))
        if overlap > 0:
            scored.append((overlap, chunk))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"text": c.text, "source": c.source, "loan_types": c.loan_types}
        for _, c in scored[:top_k]
    ]
