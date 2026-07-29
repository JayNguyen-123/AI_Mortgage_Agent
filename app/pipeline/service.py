"""
Pipeline management: the "bird-dog" logic that keeps loans moving.

Encodes:
 - Valid stage transitions (a state machine, not free-form status strings)
 - What documents/tasks unlock at each stage
 - When to auto-generate a follow-up if a task goes stale
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import (
    DocType,
    FollowUp,
    FollowUpChannel,
    LoanFile,
    LoanStage,
    PipelineTask,
    StageHistory,
    TaskStatus,
)

# Which stage transitions are legal. Prevents the agent from "skipping"
# a loan to Clear-to-Close by mistake, etc.
VALID_TRANSITIONS: dict[LoanStage, set[LoanStage]] = {
    LoanStage.LEAD: {LoanStage.APPLICATION_STARTED, LoanStage.WITHDRAWN},
    LoanStage.APPLICATION_STARTED: {LoanStage.DOCUMENT_COLLECTION, LoanStage.WITHDRAWN, LoanStage.ON_HOLD},
    LoanStage.DOCUMENT_COLLECTION: {LoanStage.PROCESSING, LoanStage.ON_HOLD, LoanStage.WITHDRAWN},
    LoanStage.PROCESSING: {LoanStage.UNDERWRITING, LoanStage.DOCUMENT_COLLECTION, LoanStage.ON_HOLD},
    LoanStage.UNDERWRITING: {
        LoanStage.CONDITIONAL_APPROVAL, LoanStage.DENIED,
        LoanStage.DOCUMENT_COLLECTION, LoanStage.ON_HOLD,
    },
    LoanStage.CONDITIONAL_APPROVAL: {LoanStage.CLEAR_TO_CLOSE, LoanStage.DOCUMENT_COLLECTION, LoanStage.ON_HOLD},
    LoanStage.CLEAR_TO_CLOSE: {LoanStage.CLOSING_SCHEDULED, LoanStage.ON_HOLD},
    LoanStage.CLOSING_SCHEDULED: {LoanStage.FUNDED, LoanStage.ON_HOLD},
    LoanStage.ON_HOLD: {  # can resume into whichever stage it was paused from
        LoanStage.DOCUMENT_COLLECTION, LoanStage.PROCESSING, LoanStage.UNDERWRITING,
        LoanStage.CONDITIONAL_APPROVAL, LoanStage.CLEAR_TO_CLOSE, LoanStage.WITHDRAWN,
    },
    LoanStage.FUNDED: set(),
    LoanStage.DENIED: set(),
    LoanStage.WITHDRAWN: set(),
}

# Standard document checklist per loan type, used to auto-generate tasks
# when a loan enters DOCUMENT_COLLECTION. Simplified / illustrative.
STANDARD_DOC_CHECKLIST: dict[str, list[DocType]] = {
    "conventional": [
        DocType.PAYSTUB, DocType.W2, DocType.BANK_STATEMENT,
        DocType.ID_DOCUMENT, DocType.CREDIT_REPORT,
    ],
    "fha": [
        DocType.PAYSTUB, DocType.W2, DocType.BANK_STATEMENT,
        DocType.ID_DOCUMENT, DocType.CREDIT_REPORT,
    ],
    "va": [
        DocType.PAYSTUB, DocType.W2, DocType.BANK_STATEMENT,
        DocType.ID_DOCUMENT, DocType.CREDIT_REPORT,
    ],
    "self_employed_conventional": [
        DocType.TAX_RETURN_1040, DocType.K1, DocType.BANK_STATEMENT,
        DocType.ID_DOCUMENT, DocType.CREDIT_REPORT,
    ],
}


class InvalidStageTransition(Exception):
    pass


def transition_stage(db: Session, loan: LoanFile, new_stage: LoanStage,
                      changed_by: str = "agent", reason: str | None = None) -> LoanFile:
    allowed = VALID_TRANSITIONS.get(loan.stage, set())
    if new_stage not in allowed:
        raise InvalidStageTransition(
            f"Cannot move loan {loan.id} from {loan.stage} to {new_stage}. "
            f"Allowed: {sorted(s.value for s in allowed)}"
        )

    history = StageHistory(
        loan_file_id=loan.id,
        from_stage=loan.stage.value,
        to_stage=new_stage.value,
        changed_by=changed_by,
        reason=reason,
    )
    loan.stage = new_stage
    loan.stage_updated_at = datetime.utcnow()

    db.add(history)
    db.add(loan)

    if new_stage == LoanStage.DOCUMENT_COLLECTION:
        _generate_doc_checklist_tasks(db, loan)

    db.commit()
    db.refresh(loan)
    return loan


def _generate_doc_checklist_tasks(db: Session, loan: LoanFile) -> None:
    checklist = STANDARD_DOC_CHECKLIST.get(loan.loan_type, STANDARD_DOC_CHECKLIST["conventional"])
    existing_doc_types = {d.doc_type for d in loan.documents}
    for doc_type in checklist:
        if doc_type in existing_doc_types:
            continue
        task = PipelineTask(
            loan_file_id=loan.id,
            task_type=f"collect_{doc_type.value}",
            description=f"Collect {doc_type.value.replace('_', ' ')} from borrower",
            due_date=datetime.utcnow() + timedelta(days=5),
        )
        db.add(task)


def get_stale_tasks(db: Session, staleness_days: int = 3) -> list[PipelineTask]:
    """Open tasks past their due date -- candidates for a follow-up."""
    cutoff = datetime.utcnow() - timedelta(days=staleness_days)
    return (
        db.query(PipelineTask)
        .filter(PipelineTask.status == TaskStatus.OPEN)
        .filter(PipelineTask.due_date < cutoff)
        .all()
    )


def schedule_followup_for_task(db: Session, task: PipelineTask,
                                channel: FollowUpChannel = FollowUpChannel.SMS) -> FollowUp:
    loan = task.loan_file
    followup = FollowUp(
        loan_file_id=loan.id,
        channel=channel,
        language=loan.borrower.preferred_language,
        scheduled_at=datetime.utcnow(),
        message_template="doc_reminder",
        context={"task_id": task.id, "task_type": task.task_type},
    )
    db.add(followup)
    db.commit()
    db.refresh(followup)
    return followup


def loan_summary(loan: LoanFile) -> dict:
    """Compact status snapshot -- what the voice agent reads back to a
    borrower or loan officer when asked "where does my loan stand?"."""
    open_tasks = [t for t in loan.tasks if t.status == TaskStatus.OPEN]
    return {
        "loan_id": loan.id,
        "borrower": loan.borrower.full_name,
        "stage": loan.stage.value,
        "loan_type": loan.loan_type,
        "loan_amount": float(loan.loan_amount) if loan.loan_amount else None,
        "target_close_date": loan.target_close_date.isoformat() if loan.target_close_date else None,
        "open_tasks": [
            {"type": t.task_type, "due": t.due_date.isoformat() if t.due_date else None}
            for t in open_tasks
        ],
        "days_since_stage_update": (datetime.utcnow() - loan.stage_updated_at).days,
    }
