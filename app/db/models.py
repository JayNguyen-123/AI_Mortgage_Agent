"""
SQLAlchemy models = the system of record for the loan pipeline.

Design notes
------------
- Every borrower-facing money/date field that drives a lending decision is
  stored with an explicit source (which document, which rule) so an
  underwriter can audit *why* the agent calculated what it calculated.
- PII (SSN, full account numbers) is deliberately NOT modeled as plain
  columns here. In production these go in a tokenized/encrypted vault
  (e.g. via a KMS-backed column encryption or a secrets service) and this
  schema only stores a reference token. See ADR-003 in README.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class Language(str, enum.Enum):
    EN = "en"
    VI = "vi"
    MIXED = "mixed"  # customer code-switches; agent detects per-turn


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    LOAN_OFFICER = "loan_officer"
    PROCESSOR = "processor"


class LoanStage(str, enum.Enum):
    LEAD = "lead"
    APPLICATION_STARTED = "application_started"
    DOCUMENT_COLLECTION = "document_collection"
    PROCESSING = "processing"
    UNDERWRITING = "underwriting"
    CONDITIONAL_APPROVAL = "conditional_approval"
    CLEAR_TO_CLOSE = "clear_to_close"
    CLOSING_SCHEDULED = "closing_scheduled"
    FUNDED = "funded"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"
    ON_HOLD = "on_hold"


class DocType(str, enum.Enum):
    PAYSTUB = "paystub"
    W2 = "w2"
    FORM_1099 = "1099"
    TAX_RETURN_1040 = "1040"
    K1 = "k1"
    BANK_STATEMENT = "bank_statement"
    CREDIT_REPORT = "credit_report"
    PURCHASE_CONTRACT = "purchase_contract"
    ID_DOCUMENT = "id_document"
    GIFT_LETTER = "gift_letter"
    OTHER = "other"


class DocStatus(str, enum.Enum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    NEEDS_REVIEW = "needs_review"
    VERIFIED = "verified"
    REJECTED = "rejected"


class TaskStatus(str, enum.Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class FollowUpChannel(str, enum.Enum):
    VOICE_CALL = "voice_call"
    SMS = "sms"
    EMAIL = "email"


class FollowUpStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Borrower(Base):
    __tablename__ = "borrowers"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    full_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    preferred_language: Mapped[Language] = mapped_column(
        Enum(Language), default=Language.EN
    )
    pii_vault_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    loan_files: Mapped[list["LoanFile"]] = relationship(back_populates="borrower")


class LoanFile(Base):
    __tablename__ = "loan_files"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    borrower_id: Mapped[str] = mapped_column(ForeignKey("borrowers.id"))
    loan_officer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    loan_type: Mapped[str] = mapped_column(String(50))
    loan_purpose: Mapped[str] = mapped_column(String(50), default="purchase")
    purchase_price: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    loan_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    property_address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    target_close_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    stage: Mapped[LoanStage] = mapped_column(Enum(LoanStage), default=LoanStage.LEAD)
    stage_updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    borrower: Mapped["Borrower"] = relationship(back_populates="loan_files")
    documents: Mapped[list["Document"]] = relationship(back_populates="loan_file")
    tasks: Mapped[list["PipelineTask"]] = relationship(back_populates="loan_file")
    follow_ups: Mapped[list["FollowUp"]] = relationship(back_populates="loan_file")
    income_items: Mapped[list["IncomeItem"]] = relationship(back_populates="loan_file")
    stage_history: Mapped[list["StageHistory"]] = relationship(back_populates="loan_file")


class StageHistory(Base):
    __tablename__ = "stage_history"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    loan_file_id: Mapped[str] = mapped_column(ForeignKey("loan_files.id"))
    from_stage: Mapped[str | None] = mapped_column(String(50), nullable=True)
    to_stage: Mapped[str] = mapped_column(String(50))
    changed_by: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    loan_file: Mapped["LoanFile"] = relationship(back_populates="stage_history")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    loan_file_id: Mapped[str] = mapped_column(ForeignKey("loan_files.id"))
    doc_type: Mapped[DocType] = mapped_column(Enum(DocType))
    storage_uri: Mapped[str] = mapped_column(String(500))
    status: Mapped[DocStatus] = mapped_column(Enum(DocStatus), default=DocStatus.UPLOADED)
    ocr_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    extracted_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    loan_file: Mapped["LoanFile"] = relationship(back_populates="documents")


class IncomeItem(Base):
    __tablename__ = "income_items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    loan_file_id: Mapped[str] = mapped_column(ForeignKey("loan_files.id"))
    income_type: Mapped[str] = mapped_column(String(50))
    monthly_qualifying_income: Mapped[float] = mapped_column(Numeric(12, 2))
    calculation_method: Mapped[str] = mapped_column(String(100))
    calculation_inputs: Mapped[dict] = mapped_column(JSON)
    source_document_ids: Mapped[dict] = mapped_column(JSON)
    flagged_for_underwriter: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    loan_file: Mapped["LoanFile"] = relationship(back_populates="income_items")


class PipelineTask(Base):
    __tablename__ = "pipeline_tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    loan_file_id: Mapped[str] = mapped_column(ForeignKey("loan_files.id"))
    task_type: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)
    assigned_to: Mapped[str] = mapped_column(String(100), default="agent")
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.OPEN)
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    loan_file: Mapped["LoanFile"] = relationship(back_populates="tasks")


class FollowUp(Base):
    __tablename__ = "follow_ups"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    loan_file_id: Mapped[str] = mapped_column(ForeignKey("loan_files.id"))
    channel: Mapped[FollowUpChannel] = mapped_column(Enum(FollowUpChannel))
    language: Mapped[Language] = mapped_column(Enum(Language), default=Language.EN)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[FollowUpStatus] = mapped_column(
        Enum(FollowUpStatus), default=FollowUpStatus.SCHEDULED
    )
    message_template: Mapped[str] = mapped_column(String(100))
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    loan_file: Mapped["LoanFile"] = relationship(back_populates="follow_ups")


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    loan_file_id: Mapped[str | None] = mapped_column(
        ForeignKey("loan_files.id"), nullable=True
    )
    borrower_id: Mapped[str | None] = mapped_column(
        ForeignKey("borrowers.id"), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(30))
    role: Mapped[str] = mapped_column(String(20))
    detected_language: Mapped[Language | None] = mapped_column(
        Enum(Language), nullable=True
    )
    content: Mapped[str] = mapped_column(Text)
    audio_storage_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    tool_call: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class User(Base):
    """Internal users only -- loan officers, processors, admins. Borrowers
    never get an account; they're only ever reached through the agent."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    hashed_password: Mapped[str] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.LOAN_OFFICER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
