"""
Admin-only routes for maintaining the guideline knowledge base.
Requires UserRole.ADMIN -- see app/auth/.
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile

from app.auth.dependencies import require_role
from app.db.models import Language, UserRole
from app.knowledge.guidelines_kb import ingest_guideline_pdf

router = APIRouter(prefix="/admin/guidelines", tags=["admin"])


@router.post("/ingest")
async def ingest_guideline(
    file: UploadFile,
    source_name: str,
    loan_types: str,  # comma-separated, e.g. "conventional,fha"
    current_user=Depends(require_role(UserRole.ADMIN)),
):
    if file.content_type != "application/pdf":
        raise HTTPException(415, "Only PDF guideline documents are supported.")

    file_bytes = await file.read()
    loan_type_list = [t.strip() for t in loan_types.split(",") if t.strip()]
    if not loan_type_list:
        raise HTTPException(400, "loan_types must include at least one of: "
                                  "conventional, fha, va, usda, jumbo")

    try:
        chunk_count = ingest_guideline_pdf(file_bytes, source=source_name, loan_types=loan_type_list)
    except ValueError as e:
        # e.g. a scanned/image PDF with no extractable text layer
        raise HTTPException(422, str(e))
    except ImportError:
        raise HTTPException(
            503,
            "Vector store dependencies not installed on this server "
            "(chromadb, sentence-transformers). Install requirements.txt.",
        )

    return {
        "status": "ingested",
        "source": source_name,
        "loan_types": loan_type_list,
        "chunks_written": chunk_count,
    }
