"""
Server-rendered dashboard for loan officers/processors/admins. Cookie
auth (see app/auth/) -- unauthenticated requests get redirected to
/dashboard/login rather than a raw 401, since a human clicking around a
browser wants a login page, not a JSON error.

All view-model shaping (dates -> display strings, enums -> labels,
"which stages can this loan move to") happens here, not in the
templates -- Jinja stays close to pure presentation.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth.security import TokenError, create_access_token, decode_access_token, verify_password
from app.db.models import LoanFile, LoanStage, TaskStatus, User
from app.db.session import get_db
from app.pipeline.service import VALID_TRANSITIONS, InvalidStageTransition, transition_stage

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory="app/dashboard/templates")

_STAGE_LABELS = {
    LoanStage.LEAD: "Lead",
    LoanStage.APPLICATION_STARTED: "Application Started",
    LoanStage.DOCUMENT_COLLECTION: "Document Collection",
    LoanStage.PROCESSING: "Processing",
    LoanStage.UNDERWRITING: "Underwriting",
    LoanStage.CONDITIONAL_APPROVAL: "Conditional Approval",
    LoanStage.CLEAR_TO_CLOSE: "Clear to Close",
    LoanStage.CLOSING_SCHEDULED: "Closing Scheduled",
    LoanStage.FUNDED: "Funded",
    LoanStage.DENIED: "Denied",
    LoanStage.WITHDRAWN: "Withdrawn",
    LoanStage.ON_HOLD: "On Hold",
}

# Board columns: the active pipeline, left to right. Terminal states
# (funded/denied/withdrawn) aren't board columns -- they're where loans
# go *to*, not where anyone works from day to day.
_BOARD_STAGES = [
    LoanStage.LEAD, LoanStage.APPLICATION_STARTED, LoanStage.DOCUMENT_COLLECTION,
    LoanStage.PROCESSING, LoanStage.UNDERWRITING, LoanStage.CONDITIONAL_APPROVAL,
    LoanStage.CLEAR_TO_CLOSE, LoanStage.CLOSING_SCHEDULED, LoanStage.ON_HOLD,
]


def _get_dashboard_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except TokenError:
        return None
    user = db.get(User, payload["sub"])
    return user if user and user.is_active else None


def _money(value) -> str:
    if value is None:
        return "--"
    return f"${float(value):,.0f}"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email.lower()).first()
    if not user or not user.is_active or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Incorrect email or password."}, status_code=401
        )

    token = create_access_token(subject=user.id, role=user.role.value)
    user.last_login_at = datetime.utcnow()
    db.add(user)
    db.commit()

    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie("access_token", token, max_age=60 * 60 * 12, httponly=True, samesite="lax", secure=True)
    return response


@router.get("", response_class=HTMLResponse)
def pipeline_board(request: Request, db: Session = Depends(get_db)):
    user = _get_dashboard_user(request, db)
    if not user:
        return RedirectResponse(url="/dashboard/login", status_code=303)

    loans = db.query(LoanFile).filter(LoanFile.stage.in_(_BOARD_STAGES)).all()
    by_stage: dict[LoanStage, list[LoanFile]] = {stage: [] for stage in _BOARD_STAGES}
    for loan in loans:
        by_stage[loan.stage].append(loan)

    now = datetime.utcnow()
    columns = []
    for stage in _BOARD_STAGES:
        card_loans = []
        for loan in by_stage[stage]:
            open_tasks = [t for t in loan.tasks if t.status == TaskStatus.OPEN]
            stale = [t for t in open_tasks if t.due_date and t.due_date < now]
            card_loans.append({
                "id": loan.id,
                "borrower_name": loan.borrower.full_name,
                "loan_type": loan.loan_type,
                "days_in_stage": (now - loan.stage_updated_at).days,
                "loan_amount_display": _money(loan.loan_amount),
                "open_task_count": len(open_tasks),
                "has_stale_task": len(stale) > 0,
            })
        columns.append({
            "stage_key": stage.value,
            "stage_label": _STAGE_LABELS[stage],
            "loans": card_loans,
        })

    return templates.TemplateResponse("board.html", {
        "request": request, "user": user, "active_nav": "board",
        "total_loans": len(loans), "columns": columns,
    })


@router.get("/loans/{loan_id}", response_class=HTMLResponse)
def loan_detail(request: Request, loan_id: str, db: Session = Depends(get_db)):
    user = _get_dashboard_user(request, db)
    if not user:
        return RedirectResponse(url="/dashboard/login", status_code=303)

    loan = db.get(LoanFile, loan_id)
    if not loan:
        return HTMLResponse("Loan not found", status_code=404)

    now = datetime.utcnow()
    documents = [
        {"doc_type": d.doc_type.value, "status": d.status.value, "uploaded_at": d.uploaded_at.strftime("%Y-%m-%d %H:%M")}
        for d in loan.documents
    ]
    income_items = [
        {"income_type": i.income_type, "monthly_display": _money(i.monthly_qualifying_income),
         "calculation_method": i.calculation_method, "flagged_for_underwriter": i.flagged_for_underwriter}
        for i in loan.income_items
    ]
    tasks = [
        {"description": t.description, "due_display": t.due_date.strftime("%Y-%m-%d") if t.due_date else "--",
         "is_overdue": bool(t.due_date and t.due_date < now)}
        for t in loan.tasks if t.status == TaskStatus.OPEN
    ]
    stage_history = [
        {"changed_at_display": h.changed_at.strftime("%Y-%m-%d %H:%M"),
         "from_stage_label": _STAGE_LABELS.get(LoanStage(h.from_stage), h.from_stage) if h.from_stage else "--",
         "to_stage_label": _STAGE_LABELS.get(LoanStage(h.to_stage), h.to_stage),
         "changed_by": h.changed_by, "reason": h.reason}
        for h in sorted(loan.stage_history, key=lambda h: h.changed_at, reverse=True)
    ]
    allowed_next_stages = [
        {"key": s.value, "label": _STAGE_LABELS[s]} for s in VALID_TRANSITIONS.get(loan.stage, set())
    ]

    return templates.TemplateResponse("loan_detail.html", {
        "request": request, "user": user,
        "loan": {"id": loan.id, "borrower_name": loan.borrower.full_name, "loan_type": loan.loan_type,
                 "loan_purpose": loan.loan_purpose, "stage_label": _STAGE_LABELS[loan.stage]},
        "documents": documents, "income_items": income_items, "tasks": tasks,
        "stage_history": stage_history, "allowed_next_stages": allowed_next_stages, "error": None,
    })


@router.post("/loans/{loan_id}/transition")
def loan_transition(request: Request, loan_id: str, new_stage: str = Form(...), db: Session = Depends(get_db)):
    user = _get_dashboard_user(request, db)
    if not user:
        return RedirectResponse(url="/dashboard/login", status_code=303)

    loan = db.get(LoanFile, loan_id)
    if not loan:
        return HTMLResponse("Loan not found", status_code=404)

    try:
        transition_stage(db, loan, LoanStage(new_stage), changed_by=user.email, reason="Changed via dashboard")
    except (InvalidStageTransition, ValueError):
        pass  # re-render the detail page; the user sees the loan didn't move

    return RedirectResponse(url=f"/dashboard/loans/{loan_id}", status_code=303)


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, db: Session = Depends(get_db)):
    from app.pipeline.service import get_stale_tasks

    user = _get_dashboard_user(request, db)
    if not user:
        return RedirectResponse(url="/dashboard/login", status_code=303)

    stale = get_stale_tasks(db)
    tasks = [
        {"borrower_name": t.loan_file.borrower.full_name, "description": t.description,
         "due_display": t.due_date.strftime("%Y-%m-%d") if t.due_date else "--", "loan_id": t.loan_file_id}
        for t in stale
    ]
    return templates.TemplateResponse("tasks.html", {
        "request": request, "user": user, "active_nav": "tasks", "tasks": tasks,
    })
