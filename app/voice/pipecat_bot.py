"""
Voice transport: Pipecat + Twilio Media Streams + Gemini Live.

Why Pipecat here instead of the hand-rolled loop in gemini_live_client.py:
Telephony needs jitter buffering, mu-law/PCM resampling, VAD, barge-in,
and reconnect handling that are genuinely hard to get right by hand.
Pipecat's GeminiMultimodalLiveLLMService + TwilioFrameSerializer handle
all of that, and the whole "LLM" node here *is* Gemini Live -- no
separate STT/TTS services are needed since it's native audio-in/audio-out.

IMPORTANT / HONESTY NOTE: this module is written against Pipecat's
documented APIs (docs.pipecat.ai, current as of this build) but was
**not executable in the sandbox this was built in** -- pipecat-ai isn't
installed there and the sandbox has no network access to install it.
The pieces that don't require pipecat (TwiML generation, tool-schema
conversion) are unit-tested in tests/test_voice_routes.py; the pipeline
wiring below needs a real local run against a Twilio trial account
before you trust it in production. Pipecat's transport/import paths
also move between versions -- pin `pipecat-ai` in requirements.txt and
diff against docs.pipecat.ai if anything here errors on import.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.agent import tools as agent_tools
from app.config import get_settings
from app.voice.function_schemas import LIVE_TOOL_DECLARATIONS
from app.voice.system_prompts import MORTGAGE_AGENT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def _tool_declarations_to_schema_kwargs() -> list[dict]:
    """
    Convert our Gemini-native LIVE_TOOL_DECLARATIONS (name/description/
    parameters.properties/parameters.required) into plain kwargs dicts
    matching pipecat.adapters.schemas.function_schema.FunctionSchema's
    constructor signature. Kept dependency-free (no pipecat import) so
    it's testable without pipecat installed -- see
    tests/test_voice_routes.py::test_tool_schema_conversion.
    """
    converted = []
    for decl in LIVE_TOOL_DECLARATIONS:
        params = decl.get("parameters", {})
        converted.append({
            "name": decl["name"],
            "description": decl["description"],
            "properties": params.get("properties", {}),
            "required": params.get("required", []),
        })
    return converted


def _build_tools_schema():
    """Build a pipecat ToolsSchema from our shared tool declarations.
    Imports pipecat lazily so this module can be imported (and the
    dependency-free helper above tested) without pipecat installed."""
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema

    schemas = [FunctionSchema(**kwargs) for kwargs in _tool_declarations_to_schema_kwargs()]
    return ToolsSchema(standard_tools=schemas)


def _make_tool_handler(db: Session, tool_name: str):
    """
    One handler per registered tool name, all delegating to the same
    app.agent.tools.dispatch() used by the raw Gemini Live client and
    the (future) text-chat channel -- this is what keeps voice, chat,
    and SMS behaving identically rather than drifting into three
    separate implementations of "what does update_pipeline_stage do."
    """
    from pipecat.services.llm_service import FunctionCallParams

    async def handler(params: FunctionCallParams) -> None:
        result = await agent_tools.dispatch(db, tool_name, dict(params.arguments))
        await params.result_callback(result)

    return handler


async def run_bot(websocket, stream_sid: str, call_sid: str, loan_id: str | None, db: Session) -> None:
    """
    Build and run the Pipecat pipeline for one live Twilio call.
    Call this from the FastAPI websocket route after parsing Twilio's
    initial "start" event (see app/api/routes_voice.py).
    """
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.serializers.twilio import TwilioFrameSerializer
    from pipecat.services.gemini_multimodal_live.gemini import GeminiMultimodalLiveLLMService
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    settings = get_settings()

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
            vad_audio_passthrough=True,
            serializer=serializer,
        ),
    )

    llm = GeminiMultimodalLiveLLMService(
        api_key=settings.gemini_api_key,  # for Vertex AI auth instead, see note below
        model=settings.gemini_live_model,
        voice_id="Aoede",
        system_instruction=MORTGAGE_AGENT_SYSTEM_PROMPT,
        tools=_build_tools_schema(),
    )
    # Note: GeminiMultimodalLiveLLMService's api_key constructor arg targets
    # the plain Gemini API. For Vertex AI auth (recommended for production
    # -- see .env.example) check the current Pipecat docs for the Vertex
    # credential path on this service, since that wiring differs from the
    # google-genai client used directly in gemini_live_client.py.

    for decl in LIVE_TOOL_DECLARATIONS:
        llm.register_function(decl["name"], _make_tool_handler(db, decl["name"]))

    pipeline = Pipeline([transport.input(), llm, transport.output()])
    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

    logger.info("Starting voice session for call_sid=%s loan_id=%s", call_sid, loan_id)
    runner = PipelineRunner()
    await runner.run(task)
