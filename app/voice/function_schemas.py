"""
Tool/function declarations exposed to the Gemini Live session.

These map 1:1 to functions in app/agent/tools.py. Keeping the JSON schema
separate from the Python implementation makes it easy to hand the same
schema to a text-based LangGraph agent (for chat/SMS channels) as well as
the Live voice session, so behavior stays consistent across channels.
"""

LIVE_TOOL_DECLARATIONS = [
    {
        "name": "get_loan_status",
        "description": "Get the current pipeline stage, open tasks, and key dates for a loan file.",
        "parameters": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string", "description": "The loan file ID"},
            },
            "required": ["loan_id"],
        },
    },
    {
        "name": "search_mortgage_guidelines",
        "description": (
            "Search the mortgage guideline knowledge base (agency and "
            "investor guides) for an authoritative answer to a guideline "
            "question. Always use this instead of answering guideline "
            "specifics from memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language guideline question"},
                "loan_type": {
                    "type": "string",
                    "enum": ["conventional", "fha", "va", "usda", "jumbo"],
                    "description": "Loan program context, if known",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "calculate_dti_preview",
        "description": (
            "Compute an indicative front-end/back-end DTI from borrower-"
            "stated numbers. Always label results as preliminary/indicative, "
            "pending document verification."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "gross_monthly_income": {"type": "number"},
                "housing_payment": {"type": "number"},
                "total_monthly_debts": {"type": "number"},
            },
            "required": ["gross_monthly_income", "housing_payment", "total_monthly_debts"],
        },
    },
    {
        "name": "request_document",
        "description": "Add a document request task to the borrower's loan file and notify them.",
        "parameters": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string"},
                "doc_type": {
                    "type": "string",
                    "enum": [
                        "paystub", "w2", "1099", "1040", "k1", "bank_statement",
                        "credit_report", "purchase_contract", "id_document",
                        "gift_letter", "other",
                    ],
                },
                "reason": {"type": "string", "description": "Why this document is needed"},
            },
            "required": ["loan_id", "doc_type"],
        },
    },
    {
        "name": "log_document_received",
        "description": "Mark that a borrower confirmed uploading/sending a document, for pipeline tracking.",
        "parameters": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string"},
                "doc_type": {"type": "string"},
            },
            "required": ["loan_id", "doc_type"],
        },
    },
    {
        "name": "schedule_followup",
        "description": "Schedule a follow-up (SMS, email, or call) to the borrower.",
        "parameters": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string"},
                "channel": {"type": "string", "enum": ["voice_call", "sms", "email"]},
                "when_iso": {"type": "string", "description": "ISO 8601 datetime for the follow-up"},
                "reason": {"type": "string"},
            },
            "required": ["loan_id", "channel", "when_iso", "reason"],
        },
    },
    {
        "name": "update_pipeline_stage",
        "description": "Move a loan file to a new pipeline stage. Must be a valid transition.",
        "parameters": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string"},
                "new_stage": {
                    "type": "string",
                    "enum": [
                        "application_started", "document_collection", "processing",
                        "underwriting", "conditional_approval", "clear_to_close",
                        "closing_scheduled", "funded", "denied", "withdrawn", "on_hold",
                    ],
                },
                "reason": {"type": "string"},
            },
            "required": ["loan_id", "new_stage", "reason"],
        },
    },
    {
        "name": "escalate_to_loan_officer",
        "description": (
            "Immediately flag this loan file for a human loan officer to "
            "call the borrower back. Use for hardship, complaints, fraud "
            "concerns, or anything outside the agent's authority."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string"},
                "reason": {"type": "string"},
                "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
            },
            "required": ["loan_id", "reason", "urgency"],
        },
    },
]
