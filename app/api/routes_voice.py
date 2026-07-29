"""
Voice channel entrypoints: Twilio webhook (inbound calls) + Twilio Media
Streams websocket (the actual audio path), plus an outbound-call trigger
used by the follow-up scheduler for FollowUpChannel.VOICE_CALL.

See app/voice/pipecat_bot.py for the Pipecat pipeline itself and its
"not executed in this sandbox" caveat -- the TwiML-building logic here
has no pipecat/DB dependency and *is* unit-tested (test_voice_routes.py).
"""
from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Depends, Form, Request, WebSocket
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Borrower, LoanFile, LoanStage
from app.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/voice", tags=["voice"])

_TERMINAL_STAGES = {LoanStage.FUNDED, LoanStage.DENIED, LoanStage.WITHDRAWN}


def build_stream_twiml(websocket_url: str, loan_id: str | None) -> str:
    """
    Build the TwiML that tells Twilio to open a Media Streams websocket
    to our server for this call. Pure string/XML building -- no pipecat
    or DB dependency, so it's fully unit-testable.
    """
    response = ET.Element("Response")
    connect = ET.SubElement(response, "Connect")
    stream = ET.SubElement(connect, "Stream", url=websocket_url)
    if loan_id:
        ET.SubElement(stream, "Parameter", name="loan_id", value=loan_id)
    return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(response, encoding="unicode")


def _find_most_recent_active_loan(db: Session, phone: str) -> LoanFile | None:
    borrower = db.query(Borrower).filter(Borrower.phone == phone).first()
    if not borrower:
        return None
    active_loans = [l for l in borrower.loan_files if l.stage not in _TERMINAL_STAGES]
    if not active_loans:
        return None
    return max(active_loans, key=lambda l: l.updated_at)


@router.post("/twilio/incoming")
async def twilio_incoming_call(request: Request, From: str = Form(...), db: Session = Depends(get_db)):
    """
    Twilio webhook for an inbound call. Looks up the caller's phone
    number against known borrowers so the agent starts the call already
    knowing which loan file it's talking about; if no match, the call
    still connects and the agent asks the caller to identify themselves
    / escalates, per the system prompt's hard rules.
    """
    loan = _find_most_recent_active_loan(db, From)
    ws_url = str(request.url_for("twilio_media_stream")).replace("http://", "wss://").replace("https://", "wss://")
    twiml = build_stream_twiml(ws_url, loan.id if loan else None)
    return Response(content=twiml, media_type="application/xml")


@router.post("/twilio/outbound-twiml")
async def twilio_outbound_twiml(request: Request, loan_id: str):
    """
    TwiML endpoint used for agent-initiated outbound calls (see
    initiate_outbound_call below) -- loan_id is already known since we
    originated the call, so it's passed as a query param rather than
    looked up by caller ID.
    """
    ws_url = str(request.url_for("twilio_media_stream")).replace("http://", "wss://").replace("https://", "wss://")
    twiml = build_stream_twiml(ws_url, loan_id)
    return Response(content=twiml, media_type="application/xml")


@router.websocket("/twilio/ws")
async def twilio_media_stream(websocket: WebSocket, db: Session = Depends(get_db)):
    """
    The actual audio path. Twilio connects here after the TwiML
    <Stream> directive above, sends a "connected" event then a "start"
    event (with our custom loan_id parameter), then a continuous stream
    of "media" events carrying base64 mu-law audio -- all of which
    Pipecat's TwilioFrameSerializer handles once we hand off to
    run_bot(). We only parse the "start" event ourselves, to pull out
    stream_sid/call_sid/loan_id before constructing the transport.
    """
    from app.voice.pipecat_bot import run_bot  # deferred: only needed once a call actually connects

    await websocket.accept()

    stream_sid = call_sid = loan_id = None
    async for raw_message in websocket.iter_text():
        event = json.loads(raw_message)
        if event.get("event") == "start":
            start = event["start"]
            stream_sid = start["streamSid"]
            call_sid = start["callSid"]
            loan_id = (start.get("customParameters") or {}).get("loan_id")
            break
        # ignore "connected" and any other pre-start events

    if not stream_sid:
        logger.warning("Twilio websocket closed before a 'start' event arrived")
        await websocket.close()
        return

    await run_bot(websocket, stream_sid, call_sid, loan_id, db)


def initiate_outbound_call(loan_id: str, to_phone: str) -> str:
    """
    Originate an outbound call via Twilio's REST API that connects back
    into our TwiML/websocket flow. Called by the follow-up scheduler for
    FollowUpChannel.VOICE_CALL follow-ups (see app/utils/notifications.py).
    Returns the Twilio call_sid.
    """
    settings = get_settings()
    if not settings.twilio_account_sid:
        raise ValueError("Twilio not configured -- set TWILIO_* in .env before scheduling voice-call follow-ups.")

    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    call = client.calls.create(
        to=to_phone,
        from_=settings.twilio_from_number,
        url=f"{settings.public_base_url}/voice/twilio/outbound-twiml?loan_id={loan_id}",
        method="POST",
    )
    return call.sid
