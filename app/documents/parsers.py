"""
Per-document-type structured parsers.

Regex/pattern-based, deliberately. Real paystubs/W2s/bank statements vary
enormously by payroll provider and bank -- this will not match every
layout. What it does do:
  - extract the fields that matter for income qualification when it can
  - compute an honest confidence score from *which required fields were
    actually found*, not just "did OCR run"
  - never silently guess a pay frequency; if it can't be determined from
    the document, that's a required field miss, which forces
    NEEDS_REVIEW rather than a wrong annualization.

Production upgrade path: replace `_extract_fields` bodies with calls to a
trained document-parsing model (e.g. a specialized income-document
parser); keep `REQUIRED_FIELDS` + the confidence scoring, since "did we
get what we need" matters regardless of extraction method.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class ExtractionResult:
    doc_subtype: str
    fields: dict
    confidence: float
    missing_required: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


REQUIRED_FIELDS = {
    "paystub": ["employer_name", "pay_period_start", "pay_period_end", "pay_frequency", "gross_pay_current"],
    "w2": ["employer_name", "tax_year", "box1_wages"],
    "bank_statement": ["bank_name", "statement_period_start", "statement_period_end", "ending_balance"],
}

_MONEY = r"\$?\s?([\d,]+\.\d{2})"
_DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%m-%d-%Y"]

# Labels that can legitimately follow a name/free-text field on the same
# OCR'd line (OCR flattens newlines to spaces, so a naive greedy capture
# for "Employer Name: X" will happily swallow the next label too --
# "Acme Manufacturing Co. Employee Name" -- unless capture is bounded to
# stop before the next known label).
_NEXT_LABEL_TERMINATORS = (
    r"Employee|Employer|Pay Period|Pay Frequency|Gross|Net Pay|Net|YTD|"
    r"Year[\s\-]to[\s\-]date|Statement Period|Ending Balance|Closing Balance|"
    r"Account Holder|Deposit|Withdrawal|Box\s*\d|Wages|Federal|Tax Year|$"
)


def _bounded_name(label_pattern: str, raw_text: str) -> str | None:
    """Capture free text after a label, stopping before the next known
    label rather than grabbing up to a fixed character count."""
    pattern = (
        label_pattern
        + r"[:\s]+([A-Za-z0-9&,.'\- ]{2,80}?)(?=\s+(?:"
        + _NEXT_LABEL_TERMINATORS
        + r"))"
    )
    m = re.search(pattern, raw_text, re.IGNORECASE)
    return m.group(1).strip().rstrip(",.") if m else None


def _parse_date(text: str) -> date | None:
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _money_to_float(s: str) -> float:
    return float(s.replace(",", ""))


def _score(doc_subtype: str, fields: dict) -> tuple[float, list[str]]:
    required = REQUIRED_FIELDS[doc_subtype]
    found = [f for f in required if fields.get(f) not in (None, "")]
    missing = [f for f in required if f not in found]
    return round(len(found) / len(required), 3), missing


def _infer_pay_frequency(start: date | None, end: date | None, raw_text: str) -> str | None:
    explicit = re.search(r"\b(weekly|bi-?weekly|semi-?monthly|monthly)\b", raw_text, re.IGNORECASE)
    if explicit:
        return explicit.group(1).lower().replace(" ", "-")

    if start and end:
        days = (end - start).days + 1
        if 6 <= days <= 8:
            return "weekly"
        if 13 <= days <= 15:
            return "bi-weekly"
        if 15 <= days <= 17:
            return "semi-monthly"
        if 27 <= days <= 32:
            return "monthly"
    return None


PAY_FREQUENCY_ANNUALIZATION = {
    "weekly": 52,
    "bi-weekly": 26,
    "semi-monthly": 24,
    "monthly": 12,
}


def parse_paystub(raw_text: str) -> ExtractionResult:
    fields: dict = {}

    name = _bounded_name(r"employer(?: name)?", raw_text)
    if name:
        fields["employer_name"] = name

    m = re.search(
        r"pay\s*period[:\s]*(?:from)?\s*([\d/\-]{6,10})\s*(?:to|-|–|through)\s*([\d/\-]{6,10})",
        raw_text, re.IGNORECASE,
    )
    if m:
        start, end = _parse_date(m.group(1)), _parse_date(m.group(2))
        if start:
            fields["pay_period_start"] = start.isoformat()
        if end:
            fields["pay_period_end"] = end.isoformat()
    else:
        start = end = None

    freq = _infer_pay_frequency(
        _parse_date(fields.get("pay_period_start", "")) if fields.get("pay_period_start") else None,
        _parse_date(fields.get("pay_period_end", "")) if fields.get("pay_period_end") else None,
        raw_text,
    )
    if freq:
        fields["pay_frequency"] = freq

    m = re.search(r"gross\s*pay(?:\s*this\s*period)?[:\s]*" + _MONEY, raw_text, re.IGNORECASE)
    if m:
        fields["gross_pay_current"] = _money_to_float(m.group(1))

    m = re.search(r"(?:gross\s*pay\s*)?ytd[:\s]*" + _MONEY, raw_text, re.IGNORECASE)
    if not m:
        m = re.search(r"year[\s\-]*to[\s\-]*date[:\s]*" + _MONEY, raw_text, re.IGNORECASE)
    if m:
        fields["gross_pay_ytd"] = _money_to_float(m.group(1))

    m = re.search(r"net\s*pay[:\s]*" + _MONEY, raw_text, re.IGNORECASE)
    if m:
        fields["net_pay_current"] = _money_to_float(m.group(1))

    name = _bounded_name(r"employee(?: name)?", raw_text)
    if name:
        fields["employee_name"] = name

    confidence, missing = _score("paystub", fields)
    notes = []
    if "pay_frequency" in missing:
        notes.append("Could not determine pay frequency -- do not auto-annualize; needs manual confirmation.")

    return ExtractionResult(doc_subtype="paystub", fields=fields, confidence=confidence,
                             missing_required=missing, notes=notes)


def parse_w2(raw_text: str) -> ExtractionResult:
    fields: dict = {}

    m = re.search(r"(20\d{2})\s*(?:form\s*)?w-?2", raw_text, re.IGNORECASE)
    if not m:
        m = re.search(r"tax\s*year[:\s]*(20\d{2})", raw_text, re.IGNORECASE)
    if m:
        fields["tax_year"] = int(m.group(1))

    name = _bounded_name(r"employer(?:'s)?\s*name", raw_text)
    if name:
        fields["employer_name"] = name

    m = re.search(r"(?:box\s*1|wages,?\s*tips,?\s*other\s*comp(?:ensation)?)[:\s]*" + _MONEY,
                  raw_text, re.IGNORECASE)
    if m:
        fields["box1_wages"] = _money_to_float(m.group(1))

    m = re.search(r"(?:box\s*2|federal\s*income\s*tax\s*withheld)[:\s]*" + _MONEY, raw_text, re.IGNORECASE)
    if m:
        fields["box2_federal_tax_withheld"] = _money_to_float(m.group(1))

    # Deliberately do NOT extract SSN even if present in the raw text --
    # this parser should never surface it into extracted_data.
    if re.search(r"\b\d{3}-\d{2}-\d{4}\b", raw_text):
        fields["ssn_redacted"] = True

    confidence, missing = _score("w2", fields)
    return ExtractionResult(doc_subtype="w2", fields=fields, confidence=confidence, missing_required=missing)


def parse_bank_statement(raw_text: str) -> ExtractionResult:
    fields: dict = {}

    m = re.search(r"([A-Za-z0-9&,.' \-]{3,60}(?:bank|credit union|financial))", raw_text, re.IGNORECASE)
    if m:
        fields["bank_name"] = m.group(1).strip()
    # Note: bank_name intentionally uses a suffix-anchored match (ends in
    # "bank"/"credit union") rather than _bounded_name, since it commonly
    # appears at the top of a statement with no preceding label at all.

    m = re.search(
        r"statement\s*period[:\s]*(?:from)?\s*([\d/\-]{6,10})\s*(?:to|-|–|through)\s*([\d/\-]{6,10})",
        raw_text, re.IGNORECASE,
    )
    if m:
        start, end = _parse_date(m.group(1)), _parse_date(m.group(2))
        if start:
            fields["statement_period_start"] = start.isoformat()
        if end:
            fields["statement_period_end"] = end.isoformat()

    m = re.search(r"(?:ending|closing)\s*balance[:\s]*" + _MONEY, raw_text, re.IGNORECASE)
    if m:
        fields["ending_balance"] = _money_to_float(m.group(1))

    name = _bounded_name(r"account\s*holder", raw_text)
    if name:
        fields["account_holder_name"] = name

    # Large-deposit flag: Fannie/Freddie-style guidance generally requires
    # sourcing any single deposit that's a large percentage of qualifying
    # income (commonly referenced around 50%) for purchase transactions.
    # This flags candidates for a human to evaluate against the actual
    # income figure -- it does not make that determination itself.
    large_deposits = [
        _money_to_float(x) for x in re.findall(r"deposit[:\s]*" + _MONEY, raw_text, re.IGNORECASE)
    ]
    if large_deposits:
        fields["deposits_found"] = large_deposits
        if max(large_deposits) >= 5000:
            fields["large_deposit_flag"] = True

    confidence, missing = _score("bank_statement", fields)
    notes = []
    if fields.get("large_deposit_flag"):
        notes.append("Large deposit detected -- source and season per guideline before counting toward assets.")

    return ExtractionResult(doc_subtype="bank_statement", fields=fields, confidence=confidence,
                             missing_required=missing, notes=notes)


PARSERS = {
    "paystub": parse_paystub,
    "w2": parse_w2,
    "bank_statement": parse_bank_statement,
}
