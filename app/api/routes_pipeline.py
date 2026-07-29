from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import require_authenticated
from app.db.models import Borrower, Language, LoanFile, LoanStage, User
from app.db.session import get_db
from app.pipeline import service as pipeline_service

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class CreateLoanRequest(BaseModel):
    borrower_name: str
    borrower_email: str | None = None
    borrower_phone: str | None = None
    preferred_language: Language = Language.EN
    loan_type: str
    loan_purpose: str = "purchase"
    purchase_price: float | None = None
    loan_amount: float | None = None


@router.post("/loans")
def create_loan(
    req: CreateLoanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_authenticated),
):
    borrower = Borrower(
        full_name=req.borrower_name,
        email=req.borrower_email,
        phone=req.borrower_phone,
        preferred_language=req.preferred_language,
    )
    db.add(borrower)
    db.flush()

    loan = LoanFile(
        borrower_id=borrower.id,
        loan_officer=current_user.full_name,
        loan_type=req.loan_type,
        loan_purpose=req.loan_purpose,
        purchase_price=req.purchase_price,
        loan_amount=req.loan_amount,
        stage=LoanStage.LEAD,
    )
    db.add(loan)
    db.commit()
    db.refresh(loan)
    return pipeline_service.loan_summary(loan)


@router.get("/loans/{loan_id}")
def get_loan(
    loan_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_authenticated),
):
    loan = db.get(LoanFile, loan_id)
    if not loan:
        raise HTTPException(404, "Loan not found")
    return pipeline_service.loan_summary(loan)


class TransitionRequest(BaseModel):
    new_stage: LoanStage
    reason: str | None = None


@router.post("/loans/{loan_id}/transition")
def transition_loan(
    loan_id: str,
    req: TransitionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_authenticated),
):
    loan = db.get(LoanFile, loan_id)
    if not loan:
        raise HTTPException(404, "Loan not found")
    try:
        # changed_by comes from the authenticated session, not a
        # client-supplied field -- an audit trail is only meaningful if
        # the "who" can't be spoofed by whoever's calling the API.
        updated = pipeline_service.transition_stage(
            db, loan, req.new_stage, changed_by=current_user.email, reason=req.reason
        )
    except pipeline_service.InvalidStageTransition as e:
        raise HTTPException(400, str(e))
    return pipeline_service.loan_summary(updated)


@router.get("/stale-tasks")
def stale_tasks(
    staleness_days: int = 3,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_authenticated),
):
    tasks = pipeline_service.get_stale_tasks(db, staleness_days=staleness_days)
    return [
        {
            "task_id": t.id,
            "loan_id": t.loan_file_id,
            "task_type": t.task_type,
            "due_date": t.due_date.isoformat() if t.due_date else None,
        }
        for t in tasks
    ]
