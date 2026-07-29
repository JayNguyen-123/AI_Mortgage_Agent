"""
Thin wrapper around the Gemini Live API (native-audio speech-to-speech)
for the voice channel. Handles session setup, streaming audio in/out, and
dispatching tool calls back into app.agent.tools.

This targets the google-genai SDK's Live API surface (bidi streaming over
websockets). Model IDs and exact method names on the SDK move fast --
pin google-genai in requirements.txt and confirm the current model id in
Google's Live API docs before deploying (see .env.example comment).

Two deployment modes supported via config:
 - Vertex AI (recommended for production: service-account auth, SLAs,
   data residency controls)
 - Gemini API key (fast local/dev iteration)
"""
import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Awaitable

from google import genai
from google.genai import types

from app.config import get_settings
from app.voice.function_schemas import LIVE_TOOL_DECLARATIONS
from app.voice.system_prompts import MORTGAGE_AGENT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

ToolHandler = Callable[[str, dict], Awaitable[dict]]


class MortgageLiveSession:
    """
    One live voice session with one borrower. `tool_handler` is an async
    function `(tool_name, args) -> result_dict` -- wire this to
    app.agent.tools.dispatch() so voice, chat, and SMS channels share the
    exact same business logic and guardrails.
    """

    def __init__(self, tool_handler: ToolHandler, loan_id: str | None = None):
        settings = get_settings()
        self._settings = settings
        self._tool_handler = tool_handler
        self._loan_id = loan_id

        if settings.google_genai_use_vertexai:
            self._client = genai.Client(
                vertexai=True,
                project=settings.google_cloud_project,
                location=settings.google_cloud_location,
            )
        else:
            self._client = genai.Client(api_key=settings.gemini_api_key)

        self._config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(
                parts=[types.Part(text=MORTGAGE_AGENT_SYSTEM_PROMPT)]
            ),
            tools=[{"function_declarations": LIVE_TOOL_DECLARATIONS}],
            # Native affective dialog + proactive audio let the model
            # modulate tone for stressed callers and decide when to
            # speak vs. stay silent -- useful for a task like "let the
            # borrower finish reading a document number back to us."
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
                )
            ),
            input_audio_transcription={},   # request transcripts for logging/compliance
            output_audio_transcription={},
        )

    async def run(self, mic_audio_stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """
        Consumes an inbound PCM16/16kHz mic-audio stream (e.g. from
        Pipecat/LiveKit/Twilio Media Streams), yields outbound PCM16/24kHz
        audio chunks to play back to the caller. Tool calls are handled
        transparently in the background.
        """
        async with self._client.aio.live.connect(
            model=self._settings.gemini_live_model, config=self._config
        ) as session:

            async def _pump_mic():
                async for chunk in mic_audio_stream:
                    await session.send_realtime_input(
                        audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                    )

            pump_task = asyncio.create_task(_pump_mic())

            try:
                async for message in session.receive():
                    if message.tool_call:
                        for fc in message.tool_call.function_calls:
                            result = await self._tool_handler(fc.name, dict(fc.args or {}))
                            await session.send_tool_response(
                                function_responses=[
                                    types.FunctionResponse(id=fc.id, name=fc.name, response=result)
                                ]
                            )
                        continue

                    if message.server_content and message.server_content.model_turn:
                        for part in message.server_content.model_turn.parts:
                            if part.inline_data:
                                yield part.inline_data.data

                    if message.server_content and message.server_content.turn_complete:
                        logger.debug("turn_complete loan_id=%s", self._loan_id)
            finally:
                pump_task.cancel()
