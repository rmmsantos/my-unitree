import base64
import json
from threading import Event, Thread
import unittest

from conversation.tools import (
    LIST_BEHAVIORS_TOOL,
    PERFORM_BEHAVIOR_TOOL,
    SEARCH_COMPANY_KNOWLEDGE_TOOL,
    CompanyKnowledgeTools,
    RobotBehaviorTools,
)
from conversation.voice import (
    PCMResampler,
    REALTIME_VOICES,
    UNITREE_AUDIO_APP_NAME,
    RealtimeVoice,
)
from robot.services import RobotBehavior


class FakeSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.response_created = Event()

    def send(self, message: str) -> None:
        event = json.loads(message)
        self.events.append(event)
        if event["type"] == "response.create":
            self.response_created.set()


class FakeOutput:
    def __init__(self) -> None:
        self.abort_count = 0
        self.start_count = 0

    def abort(self) -> None:
        self.abort_count += 1

    def start(self) -> None:
        self.start_count += 1


class FakeUnitreeAudioClient:
    def __init__(self) -> None:
        self.blocks: list[tuple[str, str, bytes]] = []
        self.stop_requests: list[str] = []
        self.played = Event()

    def PlayStream(
        self, app_name: str, stream_id: str, pcm: bytes
    ) -> tuple[int, None]:
        self.blocks.append((app_name, stream_id, pcm))
        self.played.set()
        return (0, None)

    def PlayStop(self, app_name: str) -> tuple[int, None]:
        self.stop_requests.append(app_name)
        return (0, None)


class FakeBehaviorService:
    def __init__(self) -> None:
        self.executions: list[tuple[str, float]] = []

    def list(self) -> tuple[tuple[RobotBehavior, ...], bool]:
        return (
            (
                RobotBehavior("hug", 19, True),
                RobotBehavior("clap", 17),
            ),
            True,
        )

    def execute(self, name: str, *, hold: float = 2.0) -> RobotBehavior:
        self.executions.append((name, hold))
        return RobotBehavior(name, 19, True)


class TestRealtimeVoice(unittest.TestCase):
    def test_session_configures_pcm_semantic_vad_and_ptpt(self) -> None:
        voice = RealtimeVoice(
            "key", model="test-realtime-model", voice="cedar"
        )

        event = voice.session_configuration()
        session = event["session"]
        instructions = " ".join(session["instructions"].split())

        self.assertEqual(event["type"], "session.update")
        self.assertEqual(session["model"], "test-realtime-model")
        self.assertEqual(session["output_modalities"], ["audio"])
        self.assertIn("European Portuguese", session["instructions"])
        self.assertEqual(
            session["audio"]["input"]["format"],
            {"type": "audio/pcm", "rate": 24_000},
        )
        self.assertEqual(
            session["audio"]["input"]["turn_detection"],
            {
                "type": "semantic_vad",
                "eagerness": "high",
                "create_response": True,
                "interrupt_response": True,
            },
        )
        self.assertEqual(
            session["audio"]["output"]["format"],
            {"type": "audio/pcm", "rate": 24_000},
        )
        self.assertEqual(session["audio"]["output"]["voice"], "cedar")
        self.assertEqual(session["tool_choice"], "auto")
        self.assertFalse(session["parallel_tool_calls"])
        self.assertEqual(
            [tool["name"] for tool in session["tools"]],
            [
                LIST_BEHAVIORS_TOOL,
                PERFORM_BEHAVIOR_TOOL,
                SEARCH_COMPANY_KNOWLEDGE_TOOL,
            ],
        )
        self.assertIn(
            "curated DigitalSign company knowledge",
            instructions,
        )
        self.assertIn(
            "normally answer with one short sentence",
            instructions,
        )
        self.assertIn(
            "do not recite every matching result",
            instructions,
        )
        self.assertIn(
            "asking for confirmation",
            instructions,
        )
        self.assertIn(
            "its physical body is your body",
            instructions,
        )
        self.assertIn(
            "Always speak about physical actions in the first person",
            instructions,
        )
        perform_tool = next(
            tool
            for tool in session["tools"]
            if tool["name"] == PERFORM_BEHAVIOR_TOOL
        )
        self.assertIn("no confirmation turn", perform_tool["description"])
        self.assertIn("with your Unitree G1 body", perform_tool["description"])

    def test_realtime_voice_rejects_an_unknown_voice(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported Realtime voice"):
            RealtimeVoice("key", voice="unknown")

    def test_realtime_voice_normalizes_voice_name(self) -> None:
        voice = RealtimeVoice("key", voice=" CEDAR ")

        configured = voice.session_configuration()["session"]["audio"]

        self.assertEqual(configured["output"]["voice"], "cedar")
        self.assertIn("marin", REALTIME_VOICES)

    def test_robot_tools_can_be_disabled_independently(self) -> None:
        voice = RealtimeVoice("key", robot_behavior_tools_enabled=False)

        session = voice.session_configuration()["session"]

        self.assertEqual(
            [tool["name"] for tool in session["tools"]],
            [SEARCH_COMPANY_KNOWLEDGE_TOOL],
        )

    def test_all_tools_can_be_disabled(self) -> None:
        voice = RealtimeVoice(
            "key",
            robot_behavior_tools_enabled=False,
            company_knowledge_tools_enabled=False,
        )

        session = voice.session_configuration()["session"]

        self.assertNotIn("tools", session)
        self.assertNotIn("tool_choice", session)

    def test_response_done_executes_behavior_and_returns_tool_output(self) -> None:
        service = FakeBehaviorService()
        tools = RobotBehaviorTools("eth0", service=service)
        voice = RealtimeVoice("key", robot_behavior_tools=tools)
        socket = FakeSocket()
        voice._socket = socket

        event = {
            "type": "response.done",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_hug",
                        "name": PERFORM_BEHAVIOR_TOOL,
                        "arguments": json.dumps(
                            {"behavior": "hug", "hold_seconds": 3}
                        ),
                    }
                ],
            },
        }
        voice.process_event(event)

        self.assertTrue(socket.response_created.wait(timeout=1))
        self.assertEqual(service.executions, [("hug", 3.0)])
        output_event = socket.events[0]
        self.assertEqual(output_event["type"], "conversation.item.create")
        self.assertEqual(output_event["item"]["call_id"], "call_hug")
        self.assertEqual(
            json.loads(output_event["item"]["output"]),
            {
                "ok": True,
                "behavior": "hug",
                "action_id": 19,
                "released_after_hold": True,
            },
        )
        self.assertEqual(socket.events[1], {"type": "response.create"})

        voice.process_event(event)
        self.assertEqual(service.executions, [("hug", 3.0)])

    def test_invalid_tool_arguments_are_returned_to_the_model(self) -> None:
        tools = RobotBehaviorTools("eth0", service=FakeBehaviorService())
        voice = RealtimeVoice("key", robot_behavior_tools=tools)
        socket = FakeSocket()
        voice._socket = socket

        voice._execute_tool_call(
            "call_bad",
            PERFORM_BEHAVIOR_TOOL,
            '{"behavior":"hug","hold_seconds":30}',
        )

        result = json.loads(socket.events[0]["item"]["output"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "ValueError")
        self.assertIn("between 0.1 and 10", result["error"])
        self.assertEqual(socket.events[1], {"type": "response.create"})

    def test_list_tool_returns_firmware_capabilities(self) -> None:
        tools = RobotBehaviorTools("eth0", service=FakeBehaviorService())

        result = tools.execute(LIST_BEHAVIORS_TOOL, {})

        self.assertTrue(result["ok"])
        self.assertTrue(result["confirmed_by_robot"])
        self.assertEqual(
            [behavior["name"] for behavior in result["behaviors"]],
            ["hug", "clap"],
        )

    def test_company_knowledge_returns_curated_history(self) -> None:
        tools = CompanyKnowledgeTools()

        result = tools.execute(
            SEARCH_COMPANY_KNOWLEDGE_TOOL,
            {"query": "Quando foi fundada a DigitalSign?"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["organization"], "DigitalSign")
        self.assertEqual(result["matches"][0]["id"], "history")
        self.assertIn("2001", result["matches"][0]["content"])
        self.assertTrue(result["matches"][0]["source_urls"])

    def test_company_knowledge_returns_product_details(self) -> None:
        tools = CompanyKnowledgeTools()

        result = tools.execute(
            SEARCH_COMPANY_KNOWLEDGE_TOOL,
            {"query": "O que é o DS SigningDesk?"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"][0]["id"], "product-signingdesk")
        self.assertIn("assinatura eletrónica", result["matches"][0]["content"])

    def test_company_knowledge_call_is_returned_to_realtime(self) -> None:
        voice = RealtimeVoice(
            "key",
            robot_behavior_tools_enabled=False,
        )
        socket = FakeSocket()
        voice._socket = socket

        voice._execute_tool_call(
            "call_company",
            SEARCH_COMPANY_KNOWLEDGE_TOOL,
            '{"query":"Quem é a DigitalSign?"}',
        )

        result = json.loads(socket.events[0]["item"]["output"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["organization"], "DigitalSign")
        self.assertEqual(socket.events[1], {"type": "response.create"})

    def test_unitree_uses_24khz_for_openai_input_and_output(self) -> None:
        voice = RealtimeVoice("key", audio_backend="unitree")

        audio = voice.session_configuration()["session"]["audio"]

        self.assertEqual(voice._capture_sample_rate, 16_000)
        self.assertEqual(audio["input"]["format"]["rate"], 24_000)
        self.assertEqual(audio["output"]["format"]["rate"], 24_000)

    def test_unitree_resamples_microphone_from_16khz_to_24khz(self) -> None:
        voice = RealtimeVoice("key", audio_backend="unitree")
        socket = FakeSocket()
        voice._socket = socket
        pcm_16khz = b"\x10\x00" * 160
        voice._microphone_queue.put(pcm_16khz)
        voice._microphone_queue.put(None)

        voice._stream_microphone()

        pcm_24khz = base64.b64decode(socket.events[0]["audio"])
        self.assertEqual(socket.events[0]["type"], "input_audio_buffer.append")
        self.assertGreater(len(pcm_24khz), len(pcm_16khz))

    def test_audio_delta_is_decoded_for_playback(self) -> None:
        voice = RealtimeVoice("key")
        pcm = b"\x01\x00\x02\x00"

        voice.process_event(
            {
                "type": "response.output_audio.delta",
                "item_id": "item_1",
                "content_index": 0,
                "delta": base64.b64encode(pcm).decode("ascii"),
            }
        )

        self.assertEqual(voice._playback_queue.get_nowait(), (0, "item_1", pcm))

    def test_speech_start_discards_audio_and_truncates_response(self) -> None:
        voice = RealtimeVoice("key")
        socket = FakeSocket()
        voice._socket = socket
        voice._audio_item_id = "item_1"
        voice._samples_sent = 24_000
        voice._playback_started_at = 0.0
        output = FakeOutput()
        voice._audio_output = output
        voice._playback_queue.put((0, "item_1", b"unplayed audio"))

        voice.process_event({"type": "input_audio_buffer.speech_started"})

        self.assertTrue(voice._playback_queue.empty())
        self.assertEqual(output.abort_count, 1)
        self.assertEqual(output.start_count, 1)
        self.assertEqual(socket.events[0]["type"], "conversation.item.truncate")
        self.assertEqual(socket.events[0]["item_id"], "item_1")
        self.assertEqual(socket.events[0]["audio_end_ms"], 1000)

    def test_unitree_resamples_and_sends_audio_to_speaker(self) -> None:
        client = FakeUnitreeAudioClient()
        voice = RealtimeVoice(
            "key", audio_backend="unitree", unitree_audio_client=client
        )
        pcm_24khz = b"\x01\x00" * 240
        voice.process_event(
            {
                "type": "response.output_audio.delta",
                "item_id": "item_g1",
                "content_index": 0,
                "delta": base64.b64encode(pcm_24khz).decode("ascii"),
            }
        )
        player = Thread(target=voice._play_unitree_audio)
        player.start()

        self.assertTrue(client.played.wait(timeout=1))
        voice._stop.set()
        player.join(timeout=1)

        app_name, stream_id, pcm_16khz = client.blocks[0]
        self.assertEqual(app_name, UNITREE_AUDIO_APP_NAME)
        self.assertEqual(stream_id, "item_g1")
        self.assertEqual(len(pcm_16khz), 160 * 2)

    def test_interruption_stops_unitree_speaker(self) -> None:
        client = FakeUnitreeAudioClient()
        voice = RealtimeVoice(
            "key", audio_backend="unitree", unitree_audio_client=client
        )
        socket = FakeSocket()
        voice._socket = socket
        voice._audio_item_id = "item_g1"

        voice.process_event({"type": "input_audio_buffer.speech_started"})

        self.assertEqual(client.stop_requests, [UNITREE_AUDIO_APP_NAME])
        self.assertEqual(socket.events[0]["type"], "conversation.item.truncate")


class TestPCMResampler(unittest.TestCase):
    def test_converts_pcm16_from_24khz_to_16khz(self) -> None:
        resampler = PCMResampler(24_000, 16_000)

        converted = resampler.resample(b"\x10\x00" * 240)

        self.assertEqual(len(converted), 160 * 2)


if __name__ == "__main__":
    unittest.main()
