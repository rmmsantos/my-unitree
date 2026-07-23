"""Local and Unitree audio transports for the OpenAI Realtime API."""

from __future__ import annotations

import base64
import json
from array import array
from contextlib import nullcontext
from queue import Empty, Full, Queue
import socket
import struct
import sys
from threading import Event, Lock, Thread
from time import monotonic
from typing import Protocol
from urllib.parse import urlencode

import sounddevice as sd
import websocket

from conversation.tools import (
    DEFAULT_COMPANY_KNOWLEDGE_PATH,
    CompanyKnowledgeTools,
    RobotBehaviorTools,
    ToolProvider,
)


REALTIME_INSTRUCTIONS = """You are a helpful, warm, and natural conversation partner.
Always speak European Portuguese (pt-PT), with pronunciation, vocabulary, and
sentence structures that are natural in Portugal. Keep the conversation casual:
normally answer with one short sentence, or just a few words when that is
enough. Address only what the user asked and let them lead the conversation.
Do not volunteer background information, enumerate related facts, give an
exhaustive answer, or turn every reply into an offer to help. Add detail only
when the user explicitly asks for it. Do not add introductions or recaps, and
do not read formatting, lists, or symbols aloud. If you cannot understand the
user, ask them naturally to repeat.
"""

ROBOT_TOOL_INSTRUCTIONS = """
You are embodied in the Unitree G1: its physical body is your body, not a
separate robot that you control. Always speak about physical actions in the
first person. Never say that you will ask, tell, or make the robot do something,
and never report that "the robot" completed an action. Use list_robot_behaviors
when the user asks what you can do physically. When the user explicitly asks
you to perform a behavior, call perform_robot_behavior immediately without
asking for confirmation. Do not announce or narrate the tool call; react as a
person performing the gesture would, briefly and naturally. Do not call it
merely to demonstrate, explain, suggest, or list a behavior. If the tool fails,
say naturally in the first person that you could not perform the action. Do not
claim that you moved unless the tool returned ok=true.
"""

COMPANY_TOOL_INSTRUCTIONS = """
You have a curated DigitalSign company knowledge tool. Whenever the user asks
about DigitalSign as an organization, including whether you know it, what it
does, its history, services, or presence, call search_digitalsign_knowledge
before answering. Treat its results as the authoritative company context for
this conversation. Do not fill missing company facts from general model
knowledge. Use only the smallest piece of retrieved information needed to
answer the specific question; do not recite every matching result. If the
knowledge base does not contain the answer, say so naturally.
"""

REALTIME_SAMPLE_RATE = 24_000
UNITREE_SAMPLE_RATE = 16_000
UNITREE_AUDIO_APP_NAME = "my-unitree-conversation"
REALTIME_VOICES = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
)
RECOMMENDED_REALTIME_VOICES = {"marin", "cedar"}


class UnitreeAudioClient(Protocol):
    """Subset of the G1 AudioClient used for PCM playback."""

    def PlayStream(self, app_name: str, stream_id: str, pcm_data: bytes) -> object: ...

    def PlayStop(self, app_name: str) -> object: ...


class PCMResampler:
    """Incrementally resample mono PCM16 with linear interpolation."""

    def __init__(self, source_rate: int, target_rate: int) -> None:
        self._step = source_rate / target_rate
        self._position = 0.0
        self._samples = array("h")

    def resample(self, pcm: bytes) -> bytes:
        new_samples = array("h")
        new_samples.frombytes(pcm)
        if sys.byteorder != "little":
            new_samples.byteswap()
        self._samples.extend(new_samples)

        output = array("h")
        while self._position + 1 < len(self._samples):
            left_index = int(self._position)
            fraction = self._position - left_index
            sample = round(
                self._samples[left_index] * (1.0 - fraction)
                + self._samples[left_index + 1] * fraction
            )
            output.append(max(-32_768, min(32_767, sample)))
            self._position += self._step

        consumed = int(self._position)
        if consumed:
            del self._samples[:consumed]
            self._position -= consumed
        if sys.byteorder != "little":
            output.byteswap()
        return output.tobytes()


class RealtimeVoice:
    """Run an interruptible, bidirectional realtime voice conversation."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-realtime-2.1",
        voice: str = "marin",
        instructions: str = REALTIME_INSTRUCTIONS,
        audio_backend: str = "local",
        unitree_interface_ip: str = "192.168.123.164",
        unitree_network_interface: str = "eth0",
        unitree_microphone_group: str = "239.168.123.161",
        unitree_microphone_port: int = 5555,
        audio_debug: bool = False,
        unitree_audio_client: UnitreeAudioClient | None = None,
        robot_behavior_tools_enabled: bool = True,
        robot_behavior_tools: RobotBehaviorTools | None = None,
        company_knowledge_tools_enabled: bool = True,
        company_knowledge_tools: CompanyKnowledgeTools | None = None,
        company_knowledge_path: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice.strip().lower()
        if self._voice not in REALTIME_VOICES:
            choices = ", ".join(REALTIME_VOICES)
            raise ValueError(
                f"Unsupported Realtime voice {voice!r}. "
                f"Available voices: {choices}."
            )
        self._instructions = instructions
        self._audio_backend = audio_backend.strip().lower()
        if self._audio_backend not in {"local", "unitree"}:
            raise ValueError("AUDIO_BACKEND must be 'local' or 'unitree'.")
        self._capture_sample_rate = (
            UNITREE_SAMPLE_RATE
            if self._audio_backend == "unitree"
            else REALTIME_SAMPLE_RATE
        )
        self._input_sample_rate = REALTIME_SAMPLE_RATE
        self._output_sample_rate = REALTIME_SAMPLE_RATE
        self._unitree_interface_ip = unitree_interface_ip
        self._unitree_network_interface = unitree_network_interface
        self._unitree_microphone_group = unitree_microphone_group
        self._unitree_microphone_port = unitree_microphone_port
        self._debug_audio = audio_debug
        self._unitree_audio_client = unitree_audio_client
        self._robot_behavior_tools = (
            robot_behavior_tools
            if robot_behavior_tools_enabled
            else None
        )
        if robot_behavior_tools_enabled and self._robot_behavior_tools is None:
            self._robot_behavior_tools = RobotBehaviorTools(
                unitree_network_interface,
                channel_initialized=self._audio_backend == "unitree",
            )
        self._company_knowledge_tools = (
            company_knowledge_tools
            if company_knowledge_tools_enabled
            else None
        )
        if (
            company_knowledge_tools_enabled
            and self._company_knowledge_tools is None
        ):
            self._company_knowledge_tools = CompanyKnowledgeTools(
                company_knowledge_path
                if company_knowledge_path is not None
                else DEFAULT_COMPANY_KNOWLEDGE_PATH
            )
        self._tool_providers: tuple[ToolProvider, ...] = tuple(
            provider
            for provider in (
                self._robot_behavior_tools,
                self._company_knowledge_tools,
            )
            if provider is not None
        )

        self._socket: websocket.WebSocket | None = None
        self._microphone_socket: socket.socket | None = None
        self._send_lock = Lock()
        self._state_lock = Lock()
        self._tool_execution_lock = Lock()
        self._stop = Event()
        self._microphone_queue: Queue[bytes | None] = Queue(maxsize=100)
        self._playback_queue: Queue[tuple[int, str, bytes | None]] = Queue()
        self._audio_output: sd.RawOutputStream | None = None
        self._background_error: BaseException | None = None
        self._debug_started_at = monotonic()
        self._last_debug_messages: dict[str, float] = {}
        self._microphone_bytes_received = 0
        self._microphone_bytes_sent = 0
        self._openai_bytes_received = 0
        self._unitree_bytes_sent = 0

        self._audio_generation = 0
        self._audio_item_id: str | None = None
        self._content_index = 0
        self._playback_started_at: float | None = None
        self._samples_sent = 0
        self._handled_tool_calls: set[str] = set()

    def session_configuration(self) -> dict[str, object]:
        """Return the event used to configure the Realtime session."""
        session: dict[str, object] = {
            "type": "realtime",
            "model": self._model,
            "output_modalities": ["audio"],
            "instructions": self._instructions,
            "audio": {
                "input": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": self._input_sample_rate,
                    },
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "high",
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": self._output_sample_rate,
                    },
                    "voice": self._voice,
                },
            },
        }
        if self._tool_providers:
            tool_instructions = ""
            if self._robot_behavior_tools is not None:
                tool_instructions += ROBOT_TOOL_INSTRUCTIONS
            if self._company_knowledge_tools is not None:
                tool_instructions += COMPANY_TOOL_INSTRUCTIONS
            session.update(
                {
                    "instructions": self._instructions + tool_instructions,
                    "tools": [
                        definition
                        for provider in self._tool_providers
                        for definition in provider.definitions()
                    ],
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                }
            )
        return {
            "type": "session.update",
            "session": session,
        }

    def run(self) -> None:
        """Connect audio devices and run until the user presses Ctrl+C."""
        parameters = urlencode({"model": self._model})
        url = f"wss://api.openai.com/v1/realtime?{parameters}"
        self._stop.clear()
        self._background_error = None
        self._debug(
            "Starting: "
            f"backend={self._audio_backend}, model={self._model}, "
            f"capture={self._capture_sample_rate} Hz, "
            f"OpenAI input={self._input_sample_rate} Hz, "
            f"output={self._output_sample_rate} Hz."
        )

        try:
            if self._audio_backend == "unitree":
                self._prepare_unitree()
            self._debug("Connecting to the Realtime API WebSocket…")
            self._socket = websocket.create_connection(
                url,
                header=[f"Authorization: Bearer {self._api_key}"],
                enable_multithread=True,
            )
            self._debug("WebSocket connected.")
            self._send(self.session_configuration())
            self._debug("Sent session.update event.")

            sender = Thread(target=self._stream_microphone, daemon=True)
            playback_target = (
                self._play_unitree_audio
                if self._audio_backend == "unitree"
                else self._play_local_audio
            )
            player = Thread(target=playback_target, daemon=True)
            sender.start()
            player.start()
            self._debug("Microphone and playback threads started.")

            if self._audio_backend == "unitree":
                receiver = Thread(target=self._receive_unitree_audio, daemon=True)
                receiver.start()
                self._debug("Microphone array thread started.")

            print("Session ready. Speak naturally; you can interrupt the response.")
            print("Press Ctrl+C to stop.\n")
            capture = (
                nullcontext()
                if self._audio_backend == "unitree"
                else sd.RawInputStream(
                    samplerate=self._capture_sample_rate,
                    blocksize=max(1, self._capture_sample_rate // 50),
                    channels=1,
                    dtype="int16",
                    callback=self._capture_local_audio,
                )
            )
            with capture:
                while not self._stop.is_set():
                    message = self._socket.recv()
                    if not message:
                        if self._background_error is not None:
                            raise RuntimeError(str(self._background_error))
                        raise RuntimeError("The Realtime connection was closed.")
                    self.process_event(json.loads(message))
        except websocket.WebSocketException as error:
            if self._background_error is not None:
                raise RuntimeError(
                    str(self._background_error)
                ) from self._background_error
            raise RuntimeError(f"Realtime connection failed: {error}") from error
        finally:
            self._debug("Closing the session and stopping playback.")
            self._stop.set()
            self._put_nowait(self._microphone_queue, None)
            self._playback_queue.put((self._audio_generation, "", None))
            if self._microphone_socket is not None:
                self._microphone_socket.close()
                self._microphone_socket = None
            if self._unitree_audio_client is not None:
                try:
                    self._unitree_audio_client.PlayStop(UNITREE_AUDIO_APP_NAME)
                except Exception:
                    pass
            if self._socket is not None:
                self._socket.close()
                self._socket = None

    def process_event(self, event: dict[str, object]) -> None:
        """Process a server event; public to support isolated tests."""
        event_type = event.get("type")
        if event_type == "response.output_audio.delta":
            try:
                audio = base64.b64decode(str(event["delta"]), validate=True)
                item_id = str(event["item_id"])
                content_index = int(event.get("content_index", 0))
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError(
                    "The API returned an invalid audio block."
                ) from error
            self._openai_bytes_received += len(audio)
            self._debug_periodically(
                "openai-audio",
                "OpenAI → audio: "
                f"{self._openai_bytes_received} total bytes received; "
                f"latest block={len(audio)} bytes, peak={self._pcm16_peak(audio)}.",
            )
            with self._state_lock:
                if item_id != self._audio_item_id:
                    self._audio_item_id = item_id
                    self._content_index = content_index
                    self._playback_started_at = None
                    self._samples_sent = 0
                generation = self._audio_generation
            self._playback_queue.put((generation, item_id, audio))
        elif event_type == "response.output_audio.done":
            self._debug("OpenAI → response audio complete.")
        elif event_type == "input_audio_buffer.speech_started":
            self._debug("OpenAI → speech detected; interrupting current response.")
            self._interrupt_response()
        elif event_type == "error":
            error = event.get("error")
            detail = error.get("message") if isinstance(error, dict) else error
            raise RuntimeError(f"Realtime API error: {detail or 'unknown error'}")
        elif event_type == "response.done":
            response = event.get("response")
            if isinstance(response, dict):
                self._debug(
                    "OpenAI → response.done: "
                    f"status={response.get('status')!r}, "
                    f"details={response.get('status_details')!r}."
                )
                output = response.get("output")
                if isinstance(output, list):
                    for item in output:
                        if (
                            isinstance(item, dict)
                            and item.get("type") == "function_call"
                        ):
                            self._start_tool_call(item)
            else:
                self._debug("OpenAI → response.done event.")
        elif event_type in {
            "session.created",
            "session.updated",
            "input_audio_buffer.speech_stopped",
            "input_audio_buffer.committed",
            "conversation.item.created",
            "response.created",
        }:
            self._debug(f"OpenAI → {event_type} event.")

    def _start_tool_call(self, item: dict[str, object]) -> None:
        """Validate and run one completed Realtime function call."""
        call_id = item.get("call_id")
        name = item.get("name")
        arguments_json = item.get("arguments")
        if not all(isinstance(value, str) and value for value in (call_id, name)):
            self._debug("Ignoring an invalid function_call item.", force=True)
            return
        with self._state_lock:
            if call_id in self._handled_tool_calls:
                return
            self._handled_tool_calls.add(call_id)
        worker = Thread(
            target=self._execute_tool_call,
            args=(call_id, name, arguments_json),
            daemon=True,
        )
        worker.start()

    def _execute_tool_call(
        self,
        call_id: str,
        name: str,
        arguments_json: object,
    ) -> None:
        """Execute a tool and return its output to the Realtime conversation."""
        try:
            if not isinstance(arguments_json, str):
                raise ValueError("Tool arguments are missing.")
            arguments = json.loads(arguments_json)
            provider = next(
                (
                    candidate
                    for candidate in self._tool_providers
                    if candidate.supports(name)
                ),
                None,
            )
            if provider is None:
                raise ValueError(f"Unknown conversation tool {name!r}.")
            with self._tool_execution_lock:
                result = provider.execute(name, arguments)
        except Exception as error:
            result = {
                "ok": False,
                "error": str(error),
                "error_type": type(error).__name__,
            }
        try:
            self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(
                            result,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
            )
            self._send({"type": "response.create"})
        except RuntimeError as error:
            self._report_background_error(error)

    def _capture_local_audio(
        self, data: object, _frames: int, _time: object, status: object
    ) -> None:
        if status:
            print(f"Microphone warning: {status}")
        self._put_nowait(self._microphone_queue, bytes(data))

    def _prepare_unitree(self) -> None:
        if self._unitree_audio_client is None:
            try:
                from unitree_sdk2py.core.channel import ChannelFactoryInitialize
                from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
            except ImportError as error:
                raise RuntimeError(
                    "The Unitree SDK is not installed. "
                    "Run ./scripts/install-unitree.sh first."
                ) from error
            self._debug(
                f"Starting DDS on interface {self._unitree_network_interface}…"
            )
            ChannelFactoryInitialize(0, self._unitree_network_interface)
            client = AudioClient()
            client.SetTimeout(3.0)
            client.Init()
            self._unitree_audio_client = client
            self._debug("G1 AudioClient initialized.")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_IF,
            socket.inet_aton(self._unitree_interface_ip),
        )
        sock.bind(("", self._unitree_microphone_port))
        membership = struct.pack(
            "=4s4s",
            socket.inet_aton(self._unitree_microphone_group),
            socket.inet_aton(self._unitree_interface_ip),
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.settimeout(0.5)
        self._microphone_socket = sock
        self._debug(
            "Microphone array multicast ready: "
            f"{self._unitree_microphone_group}:{self._unitree_microphone_port} "
            f"via interface/IP {self._unitree_network_interface}/"
            f"{self._unitree_interface_ip}."
        )

    def _receive_unitree_audio(self) -> None:
        if self._microphone_socket is None:
            self._report_background_error(
                RuntimeError("The G1 microphone was not initialized.")
            )
            return
        initial_deadline = monotonic() + 5.0
        received_audio = False
        while not self._stop.is_set():
            try:
                data, _source = self._microphone_socket.recvfrom(65_536)
            except socket.timeout:
                if not received_audio and monotonic() >= initial_deadline:
                    self._report_background_error(
                        RuntimeError(
                            "The G1 microphone array did not send audio. "
                            "Enable Voice Assistant > Wake-up Conversation Mode "
                            "in the Unitree app and verify the IP/interface."
                        )
                    )
                    return
                continue
            except OSError as error:
                if not self._stop.is_set():
                    self._report_background_error(error)
                return
            if len(data) % 2:
                data = data[:-1]
            if data:
                received_audio = True
                self._microphone_bytes_received += len(data)
                self._debug_periodically(
                    "microphone-received",
                    "G1 array → application: "
                    f"{self._microphone_bytes_received} total bytes; "
                    f"latest datagram={len(data)} bytes, "
                    f"peak={self._pcm16_peak(data)}, "
                    f"queue={self._microphone_queue.qsize()}.",
                )
                self._put_nowait(self._microphone_queue, data)

    @staticmethod
    def _put_nowait(queue: Queue[bytes | None], sample: bytes | None) -> None:
        try:
            queue.put_nowait(sample)
        except Full:
            # Never block an audio callback when the network is temporarily slow.
            pass

    def _stream_microphone(self) -> None:
        resampler = (
            PCMResampler(self._capture_sample_rate, self._input_sample_rate)
            if self._capture_sample_rate != self._input_sample_rate
            else None
        )
        if resampler is not None:
            self._debug(
                "Microphone resampler ready: "
                f"{self._capture_sample_rate} → {self._input_sample_rate} Hz."
            )
        while not self._stop.is_set():
            try:
                data = self._microphone_queue.get(timeout=0.1)
            except Empty:
                continue
            if data is None:
                return
            try:
                openai_data = resampler.resample(data) if resampler else data
                if not openai_data:
                    continue
                self._send(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(openai_data).decode("ascii"),
                    }
                )
                self._microphone_bytes_sent += len(openai_data)
                self._debug_periodically(
                    "microphone-sent",
                    "Application → OpenAI: "
                    f"{self._microphone_bytes_sent} total PCM/"
                    f"{self._input_sample_rate} Hz bytes sent; "
                    f"queue={self._microphone_queue.qsize()}.",
                )
            except RuntimeError as error:
                self._debug(f"Failed to send microphone audio: {error}.")
                self._stop.set()
                if self._socket is not None:
                    self._socket.close()
                return

    def _play_local_audio(self) -> None:
        try:
            with sd.RawOutputStream(
                samplerate=self._output_sample_rate, channels=1, dtype="int16"
            ) as output:
                with self._state_lock:
                    self._audio_output = output
                while not self._stop.is_set():
                    try:
                        generation, _item_id, data = self._playback_queue.get(
                            timeout=0.1
                        )
                    except Empty:
                        continue
                    with self._state_lock:
                        if generation != self._audio_generation:
                            continue
                        if data is None:
                            continue
                        if self._playback_started_at is None:
                            self._playback_started_at = monotonic()
                        output.write(data)
                        self._samples_sent += len(data) // 2
        except (OSError, sd.PortAudioError) as error:
            self._report_background_error(error)
        finally:
            with self._state_lock:
                self._audio_output = None

    def _play_unitree_audio(self) -> None:
        client = self._unitree_audio_client
        if client is None:
            self._report_background_error(
                RuntimeError("The G1 speaker was not initialized.")
            )
            return

        current_item: str | None = None
        resampler = PCMResampler(self._output_sample_rate, UNITREE_SAMPLE_RATE)
        self._debug("Unitree player ready; waiting for audio from OpenAI.")
        try:
            while not self._stop.is_set():
                try:
                    generation, item_id, data = self._playback_queue.get(timeout=0.1)
                except Empty:
                    continue
                with self._state_lock:
                    if generation != self._audio_generation or data is None:
                        continue
                    if item_id != current_item:
                        current_item = item_id
                        resampler = PCMResampler(
                            self._output_sample_rate, UNITREE_SAMPLE_RATE
                        )
                    unitree_pcm = resampler.resample(data)
                    if not unitree_pcm:
                        continue
                    if self._playback_started_at is None:
                        self._playback_started_at = monotonic()
                    result = client.PlayStream(
                        UNITREE_AUDIO_APP_NAME, item_id, unitree_pcm
                    )
                    status_code = result[0] if isinstance(result, tuple) else result
                    self._unitree_bytes_sent += len(unitree_pcm)
                    self._debug_periodically(
                        "unitree-playstream",
                        "Application → PlayStream: "
                        f"{self._unitree_bytes_sent} total PCM16/16 kHz bytes; "
                        f"latest block={len(unitree_pcm)} bytes, "
                        f"peak={self._pcm16_peak(unitree_pcm)}, "
                        f"item={item_id}, result={status_code!r}.",
                    )
                    if status_code not in (None, 0):
                        raise RuntimeError(
                            f"The G1 AudioClient returned status code {status_code}."
                        )
                    self._samples_sent += len(data) // 2
        except Exception as error:
            self._report_background_error(error)

    def _interrupt_response(self) -> None:
        with self._state_lock:
            item_id = self._audio_item_id
            content_index = self._content_index
            sent_duration = self._samples_sent / self._output_sample_rate
            elapsed_duration = (
                monotonic() - self._playback_started_at
                if self._playback_started_at is not None
                else 0.0
            )
            end_ms = round(min(sent_duration, elapsed_duration) * 1000)
            if self._audio_output is not None:
                self._audio_output.abort()
                self._audio_output.start()
            self._audio_generation += 1
            self._audio_item_id = None
            self._playback_started_at = None
            self._samples_sent = 0

        if self._unitree_audio_client is not None:
            try:
                result = self._unitree_audio_client.PlayStop(
                    UNITREE_AUDIO_APP_NAME
                )
                self._debug(f"Sent PlayStop to the G1; result={result!r}.")
            except Exception as error:
                self._report_background_error(error)

        while True:
            try:
                self._playback_queue.get_nowait()
            except Empty:
                break

        if item_id is not None:
            self._send(
                {
                    "type": "conversation.item.truncate",
                    "item_id": item_id,
                    "content_index": content_index,
                    "audio_end_ms": end_ms,
                }
            )

    def _send(self, event: dict[str, object]) -> None:
        if self._socket is None:
            raise RuntimeError("The Realtime session is not connected.")
        try:
            with self._send_lock:
                self._socket.send(json.dumps(event))
        except websocket.WebSocketException as error:
            raise RuntimeError(f"Failed to send audio: {error}") from error

    def _report_background_error(self, error: BaseException) -> None:
        self._debug(
            f"ERROR in an audio thread: {type(error).__name__}: {error}.",
            force=True,
        )
        self._background_error = error
        self._stop.set()
        if self._socket is not None:
            self._socket.close()

    def _debug(self, message: str, force: bool = False) -> None:
        if not self._debug_audio and not force:
            return
        elapsed = monotonic() - self._debug_started_at
        print(f"[audio +{elapsed:7.2f}s] {message}", flush=True)

    def _debug_periodically(
        self, key: str, message: str, interval: float = 1.0
    ) -> None:
        if not self._debug_audio:
            return
        now = monotonic()
        previous = self._last_debug_messages.get(key, 0.0)
        if now - previous >= interval:
            self._last_debug_messages[key] = now
            self._debug(message)

    @staticmethod
    def _pcm16_peak(pcm: bytes) -> int:
        if len(pcm) < 2:
            return 0
        samples = array("h")
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if sys.byteorder != "little":
            samples.byteswap()
        return max((abs(sample) for sample in samples), default=0)
