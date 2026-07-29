"""
Bilingual SMS/email dispatch for follow-ups, using the localized string
templates from app.voice.system_prompts.LOCALIZED_STRINGS so tone stays
consistent between what the voice agent says and what a text message says.
"""
import logging

from app.config import get_settings
from app.voice.system_prompts import LOCALIZED_STRINGS

logger = logging.getLogger(__name__)


def _render(template_key: str, language: str, **kwargs) -> str:
    lang = language if language in ("en", "vi") else "en"
    template = LOCALIZED_STRINGS.get(template_key, {}).get(lang)
    if not template:
        raise ValueError(f"No template '{template_key}' for language '{lang}'")
    return template.format(**kwargs)


def send_followup(followup) -> None:
    settings = get_settings()
    loan = followup.loan_file
    borrower = loan.borrower

    if followup.message_template == "doc_reminder":
        task_type = (followup.context or {}).get("task_type", "document")
        doc_type = task_type.replace("collect_", "").replace("_", " ")
        message = _render(
            "doc_reminder_sms",
            borrower.preferred_language.value,
            name=borrower.full_name,
            lender="[LENDER_NAME]",
            doc_type=doc_type,
            link="https://portal.example.com/upload",
        )
    else:
        message = f"Update on your loan {loan.id}: {(followup.context or {}).get('reason', '')}"

    if followup.channel.value == "sms":
        _send_sms(borrower.phone, message, settings)
    elif followup.channel.value == "email":
        _send_email(borrower.email, message, settings)
    elif followup.channel.value == "voice_call":
        _initiate_voice_followup(followup, borrower, settings)
    else:
        logger.warning("Unknown followup channel: %s", followup.channel.value)


def _send_sms(phone: str | None, message: str, settings) -> None:
    if not phone:
        raise ValueError("Borrower has no phone number on file")
    if not settings.twilio_account_sid:
        logger.info("[DRY RUN, no Twilio configured] SMS to %s: %s", phone, message)
        return
    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    client.messages.create(to=phone, from_=settings.twilio_from_number, body=message)


def _send_email(email: str | None, message: str, settings) -> None:
    if not email:
        raise ValueError("Borrower has no email on file")
    if not settings.sendgrid_api_key:
        logger.info("[DRY RUN, no SendGrid configured] Email to %s: %s", email, message)
        return
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    mail = Mail(from_email=settings.notify_from_email, to_emails=email,
                subject="Update on your loan", plain_text_content=message)
    SendGridAPIClient(settings.sendgrid_api_key).send(mail)


def _initiate_voice_followup(followup, borrower, settings) -> None:
    """
    Places an outbound call from the voice agent (see
    app/api/routes_voice.py::initiate_outbound_call and
    app/voice/pipecat_bot.py). This is for the agent proactively calling
    the borrower (e.g. "we're still waiting on your bank statement") --
    distinct from the borrower calling in, which goes through
    /voice/twilio/incoming instead.
    """
    if not borrower.phone:
        raise ValueError("Borrower has no phone number on file for a voice follow-up")
    if not settings.twilio_account_sid:
        logger.info("[DRY RUN, no Twilio configured] Voice call to %s for loan %s",
                    borrower.phone, followup.loan_file_id)
        return

    from app.api.routes_voice import initiate_outbound_call

    call_sid = initiate_outbound_call(loan_id=followup.loan_file_id, to_phone=borrower.phone)
    logger.info("Initiated outbound voice follow-up call_sid=%s to %s", call_sid, borrower.phone)
