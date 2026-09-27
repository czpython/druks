import asyncio
import itertools
import json
import logging

import httpx
from fastapi import WebSocket
from mcp.client.session_group import StreamableHttpParameters
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndWorkerFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.mcp_service import MCPClient
from pipecat.services.openai.realtime.events import (
    AudioConfiguration,
    AudioInput,
    AudioOutput,
    InputAudioTranscription,
    SessionProperties,
)
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.workers.runner import WorkerRunner

logger = logging.getLogger(__name__)

EVENTS_PATH = "/_external/calls/events/"
# The header that names the conversation a tool call comes from.
CONVERSATION_HEADER = "X-Druks-Conversation"
# Both realtime models speak 24 kHz audio. The Twilio serializer converts the phone
# line's 8 kHz to and from the pipeline's rates.
OUTPUT_SAMPLE_RATE = 24000
# OpenAI hears only 24 kHz audio.
OPENAI_INPUT_SAMPLE_RATE = 24000
# The local voice detector hears only 8 or 16 kHz audio, and Gemini Live takes any rate.
GEMINI_INPUT_SAMPLE_RATE = 16000


def get_model(
    *, pickup: dict, tools: ToolsSchema
) -> tuple[OpenAIRealtimeLLMService | GeminiLiveLLMService, LLMUserAggregatorParams, int]:
    """The realtime service of the Voice card's vendor, how it finds the caller's turns,
    and the audio rate it hears. The model reads the prompt, and then the facts."""
    voice = pickup["voice"]
    vendor, _, model_id = voice["model"].partition("/")
    instructions = f"{pickup['prompt']}\n\n{json.dumps(pickup['facts'], ensure_ascii=False)}"
    no_speech_seconds = pickup["limits"]["no_speech_seconds"]
    if vendor == "openai":
        # OpenAI gives the caller's words as text only when the session names a model to
        # transcribe them.
        transcription = InputAudioTranscription(model=voice["transcription_model"])
        audio = AudioConfiguration(
            input=AudioInput(transcription=transcription),
            output=AudioOutput(voice=voice["voice_name"] or None),
        )
        settings = OpenAIRealtimeLLMService.Settings(
            model=model_id,
            system_instruction=instructions,
            session_properties=SessionProperties(audio=audio, tools=tools),
        )
        # OpenAI's own voice detection reports the caller's turns.
        return (
            OpenAIRealtimeLLMService(api_key=voice["key"], settings=settings),
            LLMUserAggregatorParams(user_idle_timeout=no_speech_seconds),
            OPENAI_INPUT_SAMPLE_RATE,
        )
    # The Voice card takes only an OpenAI or a Google model.
    settings = GeminiLiveLLMService.Settings(model=model_id, system_instruction=instructions)
    if voice["voice_name"]:
        settings.voice = voice["voice_name"]
    # Gemini Live reports no turns of the caller, and the silence timer needs them.
    return (
        GeminiLiveLLMService(api_key=voice["key"], settings=settings, tools=tools),
        LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(), user_idle_timeout=no_speech_seconds
        ),
        GEMINI_INPUT_SAMPLE_RATE,
    )


class Call:
    """One live call: its token and the lines it posts to Druks. The pickup's keys live in
    this call only."""

    def __init__(self, *, druks: httpx.AsyncClient, token: str) -> None:
        self.druks = druks
        self.token = token
        self.lines: asyncio.Queue[dict] = asyncio.Queue()
        self.sequence = itertools.count(1)
        self.caller_sequence = 0
        self.assistant_sequence = 0

    async def post(self, action: str, **fields) -> httpx.Response:
        return await self.druks.post(
            EVENTS_PATH, json={"action": action, "token": self.token, **fields}
        )

    def add_line(self, *, sequence: int, role: str, text: str) -> None:
        # A turn that the caller cut off before its first word said nothing.
        if text:
            self.lines.put_nowait({"sequence": sequence, "role": role, "text": text})

    async def post_lines(self) -> None:
        """Post the lines one at a time, each under its sequence. Druks orders a call's
        lines by their sequence, so a line may arrive after a later one."""
        while True:
            line = await self.lines.get()
            try:
                response = await self.post("line", **line)
                response.raise_for_status()
            except httpx.HTTPError as error:
                logger.warning("Druks did not save line %s of a call: %s", line["sequence"], error)
            self.lines.task_done()

    async def end_call(self, params: FunctionCallParams) -> None:
        """The model's tool: the call ends after the words already said."""
        await params.result_callback({"ended": True})
        await params.llm.push_frame(EndWorkerFrame())

    async def run(self, *, websocket: WebSocket, pickup: dict) -> None:
        """Run the call with the voice model until it ends, then tell Druks."""
        _, call_data = await parse_telephony_websocket(websocket)
        serializer = TwilioFrameSerializer(
            stream_sid=call_data["stream_id"],
            params=TwilioFrameSerializer.InputParams(auto_hang_up=False),
        )
        transport = FastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                add_wav_header=False,
                serializer=serializer,
            ),
        )
        mcp = pickup["mcp"]
        headers = {
            "Authorization": f"Bearer {mcp['bearer']}",
            CONVERSATION_HEADER: mcp["conversation"],
        }
        server_params = StreamableHttpParameters(url=mcp["url"], headers=headers)
        async with MCPClient(server_params=server_params) as druks_mcp:
            end_call = FunctionSchema(
                name="end_call",
                description="End the call. Say goodbye first.",
                properties={},
                required=[],
                handler=self.end_call,
            )
            druks_tools = await druks_mcp.tools()
            tools = ToolsSchema(standard_tools=[*druks_tools.standard_tools, end_call])
            model_service, caller_params, input_sample_rate = get_model(pickup=pickup, tools=tools)
            caller_aggregator, assistant_aggregator = LLMContextAggregatorPair(
                context=LLMContext(), user_params=caller_params
            )
            worker = PipelineWorker(
                pipeline=Pipeline(
                    [
                        transport.input(),
                        caller_aggregator,
                        model_service,
                        transport.output(),
                        assistant_aggregator,
                    ]
                ),
                params=PipelineParams(
                    audio_in_sample_rate=input_sample_rate,
                    audio_out_sample_rate=OUTPUT_SAMPLE_RATE,
                ),
                # A model that cannot connect ends the call instead of leaving silence.
                processor_unusable_policy=ProcessorUnusablePolicy.END,
            )
            runner = WorkerRunner(handle_sigint=False)
            await runner.add_workers(worker)

            @transport.event_handler("on_client_connected")
            async def greet(transport, client) -> None:
                await worker.queue_frames([LLMRunFrame()])

            @transport.event_handler("on_client_disconnected")
            async def hang_up(transport, client) -> None:
                await runner.cancel()

            @caller_aggregator.event_handler("on_user_turn_idle")
            async def end_on_silence(aggregator) -> None:
                await worker.stop_when_done()

            async def end_at_cap() -> None:
                await asyncio.sleep(pickup["limits"]["max_call_seconds"])
                await worker.stop_when_done()

            @assistant_aggregator.event_handler("on_assistant_turn_started")
            async def number_the_turn(aggregator) -> None:
                # Pipecat adds the caller's words after the reply they led to has
                # started, so the caller's line takes its sequence here, before the reply.
                self.caller_sequence = next(self.sequence)
                self.assistant_sequence = next(self.sequence)

            @caller_aggregator.event_handler("on_user_turn_message_added")
            async def add_caller_line(aggregator, message) -> None:
                sequence = self.caller_sequence or next(self.sequence)
                self.add_line(sequence=sequence, role="user", text=message.content)
                self.caller_sequence = 0

            @assistant_aggregator.event_handler("on_assistant_turn_stopped")
            async def add_assistant_line(aggregator, message) -> None:
                self.add_line(
                    sequence=self.assistant_sequence, role="assistant", text=message.content
                )

            line_poster = asyncio.create_task(self.post_lines())
            cap_timer = asyncio.create_task(end_at_cap())
            try:
                await runner.run()
            finally:
                cap_timer.cancel()
                await self.lines.join()
                line_poster.cancel()
                await self.post("ended")
