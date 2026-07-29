"""
Single implementation of every agent tool, shared by the Gemini Live
voice session, the LangGraph text-chat agent, and SMS. This is where
guardrails actually get enforced in code (not just in a prompt).
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import DocType, FollowUpChannel, LoanStage, TaskStatus
from app.documents.income_calculator import calc_dti
from app.pipeline import service as pipeline_service

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """Raised for user-facing tool errors; message is safe to speak aloud."""


async def dispatch(db: Session, tool_name: str, args: dict) -> dict:
    """Route a tool call by name. Central place to log every tool
    invocation for compliance/audit."""
    logger.info("tool_call name=%s args=%s", tool_name, {k: v for k, v in args.items()})

    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool: {tool_name}"}

    try:
        return await handler(db, args)
    except ToolError as e:
        return {"error": str(e)}
    except pipeline_service.InvalidStageTransition as e:
        return {"error": str(e)}


async def _get_loan_status(db: Session, args: dict) -> dict:
    from app.db.models import LoanFile

    loan = db.get(LoanFile, args["loan_id"])
    if not loan:
        raise ToolError("I couldn't find a loan file with that ID.")
    return pipeline_service.loan_summary(loan)


async def _search_mortgage_guidelines(db: Session, args: dict) -> dict:
    from app.knowledge.guidelines_kb import search_guidelines

    results = search_guidelines(args["query"], loan_type=args.get("loan_type"))
    return {"query": args["query"], "results": results}


async def _calculate_dti_preview(db: Session, args: dict) -> dict:
    result = calc_dti(
        total_monthly_debts=args["total_monthly_debts"],
        housing_payment=args["housing_payment"],
        gross_monthly_income=args["gross_monthly_income"],
    )
    result["disclaimer"] = "Indicative only, based on borrower-stated figures -- not verified against documentation."
    return result


async def _request_document(db: Session, args: dict) -> dict:
    from app.db.models import LoanFile, PipelineTask

    loan = db.get(LoanFile, args["loan_id"])
    if not loan:
        raise ToolError("I couldn't find that loan file.")

    doc_type = args["doc_type"]
    task = PipelineTask(
        loan_file_id=loan.id,
        task_type=f"collect_{doc_type}",
        description=args.get("reason", f"Collect {doc_type} from borrower"),
    )
    db.add(task)
    db.commit()
    return {"status": "task_created", "task_type": task.task_type}


async def _log_document_received(db: Session, args: dict) -> dict:
    from app.db.models import LoanFile, PipelineTask

    loan = db.get(LoanFile, args["loan_id"])
    if not loan:
        raise ToolError("I couldn't find that loan file.")

    matching = [
        t for t in loan.tasks
        if t.task_type == f"collect_{args['doc_type']}" and t.status == TaskStatus.OPEN
    ]
    for t in matching:
        t.status = TaskStatus.DONE
        t.completed_at = datetime.utcnow()
        db.add(t)
    db.commit()
    return {"status": "logged", "tasks_closed": len(matching)}


async def _schedule_followup(db: Session, args: dict) -> dict:
    from app.db.models import FollowUp, LoanFile

    loan = db.get(LoanFile, args["loan_id"])
    if not loan:
        raise ToolError("I couldn't find that loan file.")

    followup = FollowUp(
        loan_file_id=loan.id,
        channel=FollowUpChannel(args["channel"]),
        language=loan.borrower.preferred_language,
        scheduled_at=datetime.fromisoformat(args["when_iso"]),
        message_template="manual_followup",
        context={"reason": args.get("reason", "")},
    )
    db.add(followup)
    db.commit()
    return {"status": "scheduled", "followup_id": followup.id}


async def _update_pipeline_stage(db: Session, args: dict) -> dict:
    from app.db.models import LoanFile

    loan = db.get(LoanFile, args["loan_id"])
    if not loan:
        raise ToolError("I couldn't find that loan file.")

    updated = pipeline_service.transition_stage(
        db, loan, LoanStage(args["new_stage"]),
        changed_by="agent", reason=args.get("reason"),
    )
    return {"status": "updated", "new_stage": updated.stage.value}


async def _escalate_to_loan_officer(db: Session, args: dict) -> dict:
    from app.db.models import LoanFile, PipelineTask

    loan = db.get(LoanFile, args["loan_id"])
    if not loan:
        raise ToolError("I couldn't find that loan file.")

    task = PipelineTask(
        loan_file_id=loan.id,
        task_type="human_escalation",
        assigned_to="loan_officer",
        description=f"[{args.get('urgency', 'normal').upper()}] {args['reason']}",
    )
    db.add(task)
    db.commit()
    # In production: also fire a real-time page/Slack alert here.
    return {"status": "escalated", "task_id": task.id}


_HANDLERS = {
    "get_loan_status": _get_loan_status,
    "search_mortgage_guidelines": _search_mortgage_guidelines,
    "calculate_dti_preview": _calculate_dti_preview,
    "request_document": _request_document,
    "log_document_received": _log_document_received,
    "schedule_followup": _schedule_followup,
    "update_pipeline_stage": _update_pipeline_stage,
    "escalate_to_loan_officer": _escalate_to_loan_officer,
}
