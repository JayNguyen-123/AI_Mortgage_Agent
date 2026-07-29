from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.auth.dependencies import require_authenticated
from app.db.models import Document, DocStatus, DocType, LoanFile, User
from app.db.session import get_db
from app.documents.analyzer import process_document
from app.documents.storage import get_storage_backend

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/loans/{loan_id}/upload")
async def upload_document(
    loan_id: str,
    doc_type: DocType,
    file: UploadFile,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_authenticated),
):
    loan = db.get(LoanFile, loan_id)
    if not loan:
        raise HTTPException(404, "Loan not found")

    if file.content_type not in (
        "application/pdf", "image/jpeg", "image/png", "image/tiff",
    ):
        raise HTTPException(415, f"Unsupported file type: {file.content_type}")

    file_bytes = await file.read()
    max_size_mb = 15
    if len(file_bytes) > max_size_mb * 1024 * 1024:
        raise HTTPException(413, f"File too large (max {max_size_mb}MB)")

    storage = get_storage_backend()
    storage_uri = storage.save(file_bytes, loan_id, doc_type.value, file.filename)

    document = Document(
        loan_file_id=loan_id,
        doc_type=doc_type,
        storage_uri=storage_uri,
        status=DocStatus.UPLOADED,
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    processed = process_document(db, document)
    return {
        "document_id": processed.id,
        "status": processed.status.value,
        "extracted_data": processed.extracted_data,
        "ocr_confidence": float(processed.ocr_confidence) if processed.ocr_confidence else None,
    }


@router.get("/loans/{loan_id}")
def list_documents(
    loan_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_authenticated),
):
    loan = db.get(LoanFile, loan_id)
    if not loan:
        raise HTTPException(404, "Loan not found")
    return [
        {
            "document_id": d.id,
            "doc_type": d.doc_type.value,
            "status": d.status.value,
            "uploaded_at": d.uploaded_at.isoformat(),
        }
        for d in loan.documents
    ]
