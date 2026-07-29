"""
Document analysis orchestration: storage -> OCR -> structured parse ->
income calculation -> IncomeItem, with document status routed to
NEEDS_REVIEW whenever confidence or a cross-check falls short.

Nothing in this file issues a lending decision -- `flagged_for_underwriter`
is the operative signal throughout, and defaults to True whenever there's
any ambiguity (single-page paystub, undetermined pay frequency, YTD
mismatch, etc).
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.db.models import Document, DocStatus, DocType, IncomeItem
from app.documents import ocr, parsers
from app.documents.income_calculator import calc_base_w2_income
from app.documents.storage import get_storage_backend

logger = logging.getLogger(__name__)

# Confidence threshold below which a document goes to NEEDS_REVIEW even
# if every required field happened to be found (protects against a
# document that's the wrong type entirely, or badly mis-OCR'd but still
# regex-matched something by coincidence).
MIN_CONFIDENCE_FOR_AUTO_EXTRACT = 0.75

_DOC_TYPE_TO_PARSER_KEY = {
    DocType.PAYSTUB: "paystub",
    DocType.W2: "w2",
    DocType.BANK_STATEMENT: "bank_statement",
}


def process_document(db: Session, document: Document) -> Document:
    document.status = DocStatus.PROCESSING
    db.add(document)
    db.commit()

    storage = get_storage_backend()
    try:
        file_bytes = storage.load(document.storage_uri)
    except Exception:
        logger.exception("Could not load document %s from storage", document.id)
        document.status = DocStatus.NEEDS_REVIEW
        document.review_notes = "Could not retrieve file from storage -- check upload."
        db.add(document)
        db.commit()
        return document

    filename = document.storage_uri.rsplit("/", 1)[-1]
    try:
        doc_text = ocr.extract(file_bytes, filename)
    except ValueError as e:
        document.status = DocStatus.NEEDS_REVIEW
        document.review_notes = str(e)
        db.add(document)
        db.commit()
        return document

    parser_key = _DOC_TYPE_TO_PARSER_KEY.get(document.doc_type)
    if parser_key is None:
        # No structured parser for this doc type yet (credit report,
        # purchase contract, gift letter, ID, etc). Store OCR text for a
        # human to review rather than pretending we extracted structure.
        document.extracted_data = {
            "raw_text_preview": doc_text.full_text[:1000],
            "ocr_overall_confidence": doc_text.overall_confidence,
        }
        document.ocr_confidence = doc_text.overall_confidence
        document.status = DocStatus.NEEDS_REVIEW
        document.review_notes = f"No structured parser implemented for {document.doc_type.value} yet."
        db.add(document)
        db.commit()
        db.refresh(document)
        return document

    result = parsers.PARSERS[parser_key](doc_text.full_text)
    # Combine OCR image-quality confidence with field-completeness
    # confidence -- both need to be good for auto-extraction to stand.
    combined_confidence = round(min(doc_text.overall_confidence, result.confidence), 3)

    document.extracted_data = {
        "fields": result.fields,
        "field_completeness_confidence": result.confidence,
        "ocr_confidence": doc_text.overall_confidence,
        "missing_required_fields": result.missing_required,
        "notes": result.notes,
    }
    document.ocr_confidence = combined_confidence

    if combined_confidence < MIN_CONFIDENCE_FOR_AUTO_EXTRACT or result.missing_required:
        document.status = DocStatus.NEEDS_REVIEW
        document.review_notes = (
            f"Missing required fields: {result.missing_required}" if result.missing_required
            else f"Low confidence ({combined_confidence})"
        )
    else:
        document.status = DocStatus.EXTRACTED
        if document.doc_type == DocType.PAYSTUB:
            _create_income_item_from_paystub(db, document, result.fields)

    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def _create_income_item_from_paystub(db: Session, document: Document, fields: dict) -> None:
    frequency = fields.get("pay_frequency")
    gross_current = fields.get("gross_pay_current")
    if not frequency or not gross_current:
        return  # required-field gate upstream should prevent this, but stay defensive

    annualization_factor = parsers.PAY_FREQUENCY_ANNUALIZATION.get(frequency)
    if not annualization_factor:
        return

    annual_from_current_period = gross_current * annualization_factor
    income_result = calc_base_w2_income(hourly_rate=None, annual_salary=annual_from_current_period)

    notes = [f"Annualized from current-period gross using detected frequency: {frequency}."]
    flagged = False

    # Cross-check against YTD figure when available: if the pay-period
    # count implied by YTD/current-period gross doesn't roughly match the
    # calendar-implied pay-period count for the stated period end date,
    # something is off (raise, missed periods, wrong frequency read).
    ytd = fields.get("gross_pay_ytd")
    period_end = fields.get("pay_period_end")
    if ytd and gross_current:
        implied_periods_paid = ytd / gross_current
        notes.append(f"YTD/current-period ratio implies ~{implied_periods_paid:.1f} pay periods paid so far this year.")
        if period_end:
            from datetime import date as _date
            end_date = _date.fromisoformat(period_end)
            days_into_year = (end_date - _date(end_date.year, 1, 1)).days + 1
            expected_periods = days_into_year / (365 / annualization_factor)
            if abs(implied_periods_paid - expected_periods) > max(2, expected_periods * 0.15):
                flagged = True
                notes.append(
                    f"YTD figure implies a different pay-period count than the calendar "
                    f"would suggest (~{expected_periods:.1f} expected) -- confirm frequency "
                    f"and check for a raise or missed pay period."
                )

    income_item = IncomeItem(
        loan_file_id=document.loan_file_id,
        income_type=income_result.income_type,
        monthly_qualifying_income=income_result.monthly_qualifying_income,
        calculation_method=income_result.calculation_method + f" (frequency={frequency}, from OCR-extracted paystub)",
        calculation_inputs={**income_result.inputs, **fields},
        source_document_ids={"ids": [document.id]},
        flagged_for_underwriter=flagged or True,  # single paystub is always underwriter-reviewed pre-verification
        notes=" ".join(notes),
    )
    db.add(income_item)
