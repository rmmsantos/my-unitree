from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from my_unitree.configuration import find_project_root
from robot.services import (
    UNITREE_AUDIO_APP_NAME,
    _capture_v4l2_frame,
    camera_device,
    execute_robot_behavior,
    get_robot_mode,
    list_robot_behaviors,
    list_robot_modes,
    read_robot_state,
    record_microphone,
    record_video,
    resolve_robot_mode,
    set_robot_mode,
    set_robot_volume,
    take_photo,
    test_speakers as play_speaker_test,
)
from diagnostics.cli import (
    DEFAULT_MICROPHONE_OUTPUT,
    DEFAULT_PHOTO_OUTPUT,
    DEFAULT_STATE_OUTPUT,
    DEFAULT_VIDEO_OUTPUT,
    RESULT_DIR,
    build_parser,
)
from robot.cli import build_parser as build_robot_parser


class FakeSpeakerClient:
    def __init__(self) -> None:
        self.blocks: list[tuple[str, str, bytes]] = []
        self.stopped: list[str] = []

    def PlayStream(
        self, app_name: str, stream_id: str, pcm_data: bytes
    ) -> tuple[int, None]:
        self.blocks.append((app_name, stream_id, pcm_data))
        return (0, None)

    def PlayStop(self, app_name: str) -> tuple[int, None]:
        self.stopped.append(app_name)
        return (0, None)


class FakeMicrophoneSocket:
    def setsockopt(self, *_args: object) -> None:
        pass

    def bind(self, _address: object) -> None:
        pass

    def settimeout(self, _timeout: float) -> None:
        pass

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        return b"\x01\x00" * 1_000, ("192.168.123.1", 5555)

    def close(self) -> None:
        pass


class FakeVideoClient:
    def GetImageSample(self) -> tuple[int, list[int]]:
        return (0, [1, 2, 3])


class FakeAudioClient(FakeSpeakerClient):
    def __init__(self, volume: int) -> None:
        super().__init__()
        self.volume = volume
        self.events: list[tuple[str, object]] = []

    def GetVolume(self) -> tuple[int, int]:
        self.events.append(("get", self.volume))
        return (0, self.volume)

    def SetVolume(self, volume: int) -> int:
        self.events.append(("set", volume))
        self.volume = volume
        return 0

    def PlayStream(
        self, app_name: str, stream_id: str, pcm_data: bytes
    ) -> tuple[int, None]:
        self.events.append(("play", len(pcm_data)))
        return super().PlayStream(app_name, stream_id, pcm_data)

    def PlayStop(self, app_name: str) -> tuple[int, None]:
        self.events.append(("stop", app_name))
        return super().PlayStop(app_name)


class FakeStateReader:
    def __init__(self, sample: object) -> None:
        self.sample = sample
        self.timeouts: list[float | None] = []

    def Read(self, timeout: float | None = None) -> object:
        self.timeouts.append(timeout)
        return self.sample


class FakeBehaviorClient:
    def __init__(self, action_data: object) -> None:
        self.action_data = action_data
        self.executed: list[int] = []

    def GetActionList(self) -> tuple[int, object]:
        return (0, self.action_data)

    def ExecuteAction(self, action_id: int) -> int:
        self.executed.append(action_id)
        return 0


class FakeFsmClient:
    def __init__(self, fsm_id: int) -> None:
        self.fsm_id = fsm_id
        self.requested: list[int] = []

    def GetFsmId(self) -> tuple[int, int]:
        return (0, self.fsm_id)

    def SetFsmId(self, fsm_id: int) -> int:
        self.requested.append(fsm_id)
        self.fsm_id = fsm_id
        return 0


class FakeFrame:
    shape = (360, 640, 3)


class FakeCV2:
    def __init__(self) -> None:
        self.outputs: list[Path] = []

    def imwrite(self, output: str, _frame: object) -> bool:
        path = Path(output)
        path.write_bytes(b"image")
        self.outputs.append(path)
        return True


class FakeVideoWriter:
    def __init__(self) -> None:
        self.frames: list[object] = []
        self.released = False

    def isOpened(self) -> bool:
        return True

    def write(self, frame: object) -> None:
        self.frames.append(frame)

    def release(self) -> None:
        self.released = True


class FakeVideoCV2:
    def __init__(self, writer: FakeVideoWriter) -> None:
        self.writer = writer

    @staticmethod
    def VideoWriter_fourcc(*_codec: str) -> int:
        return 0

    def VideoWriter(self, *_args: object) -> FakeVideoWriter:
        return self.writer


class TestRobot(unittest.TestCase):
    def test_project_root_is_discovered_from_a_nested_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").touch()
            nested = root / "one" / "two"
            nested.mkdir(parents=True)

            self.assertEqual(find_project_root(nested), root.resolve())

    def test_microphone_recording_has_the_exact_requested_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "microphone.wav"
            with patch(
                "robot.services.socket.socket",
                return_value=FakeMicrophoneSocket(),
            ):
                samples = record_microphone(
                    0.01,
                    output,
                    "192.168.123.164",
                    "239.168.123.161",
                    5555,
                )

            with wave.open(str(output), "rb") as recording:
                self.assertEqual(recording.getnframes(), 160)
            self.assertEqual(samples, 160)

    def test_camera_device_accepts_index_or_path(self) -> None:
        self.assertEqual(camera_device("2"), 2)
        self.assertEqual(camera_device("/dev/video4"), "/dev/video4")

    def test_raw_camera_uses_verified_v4l2_format(self) -> None:
        raw_frame = b"\x01\x02" * 12

        def capture(command: list[str], **_kwargs: object) -> object:
            output = Path(command[-1].split("=", 1)[1])
            output.write_bytes(raw_frame)
            return subprocess.CompletedProcess(command, 0, "")

        with patch(
            "robot.services.subprocess.run",
            side_effect=capture,
        ) as run:
            frame = _capture_v4l2_frame(
                "/dev/video0",
                "0x2036315a",
                4,
                3,
                2,
            )

        self.assertEqual(frame, raw_frame)
        command = run.call_args.args[0]
        self.assertIn("--device=/dev/video0", command)
        self.assertIn(
            "--set-fmt-video=width=4,height=3,pixelformat=0x2036315a",
            command,
        )

    def test_raw_camera_reports_v4l2_busy_error(self) -> None:
        result = subprocess.CompletedProcess(
            ["v4l2-ctl"],
            1,
            "VIDIOC_REQBUFS returned -1 (Device or resource busy)",
        )
        with (
            patch(
                "robot.services.subprocess.run",
                return_value=result,
            ),
            self.assertRaisesRegex(RuntimeError, "Device or resource busy"),
        ):
            _capture_v4l2_frame(
                "/dev/video0",
                "0x2036315a",
                4,
                3,
                2,
            )

    def test_photo_uses_the_unitree_video_client(self) -> None:
        cv2 = FakeCV2()
        client = FakeVideoClient()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "photo.jpg"
            with patch(
                "robot.services._read_unitree_video_frame",
                return_value=(cv2, FakeFrame()),
            ) as read_frame:
                dimensions = take_photo(
                    output,
                    "eth0",
                    client=client,
                )

            self.assertEqual(output.read_bytes(), b"image")

        self.assertEqual(dimensions, (640, 360))
        read_frame.assert_called_once_with(client)

    def test_video_uses_the_unitree_video_client(self) -> None:
        writer = FakeVideoWriter()
        cv2 = FakeVideoCV2(writer)
        client = FakeVideoClient()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "video.mp4"
            with (
                patch(
                    "robot.services._read_unitree_video_frame",
                    return_value=(cv2, FakeFrame()),
                ) as read_frame,
                patch(
                    "robot.services.time.monotonic",
                    side_effect=[0.0, 0.0, 1.0, 1.0],
                ),
            ):
                result = record_video(
                    1.0,
                    output,
                    "eth0",
                    fps=1.0,
                    client=client,
                )

        self.assertEqual(result, (1, 640, 360))
        self.assertEqual(writer.frames, [read_frame.return_value[1]])
        self.assertTrue(writer.released)
        read_frame.assert_called_once_with(client)

    def test_speaker_test_streams_and_stops_wav(self) -> None:
        client = FakeSpeakerClient()
        with tempfile.TemporaryDirectory() as directory:
            audio_file = Path(directory) / "recording.wav"
            with wave.open(str(audio_file), "wb") as recording:
                recording.setnchannels(1)
                recording.setsampwidth(2)
                recording.setframerate(16_000)
                recording.writeframes(b"\x01\x00" * 4_000)

            samples = play_speaker_test(
                audio_file,
                "eth0",
                client=client,
                sleep=lambda _duration: None,
            )

        self.assertEqual(samples, 4_000)
        self.assertEqual(len(client.blocks), 3)
        self.assertTrue(
            all(block[0] == UNITREE_AUDIO_APP_NAME for block in client.blocks)
        )
        self.assertEqual(client.stopped, [UNITREE_AUDIO_APP_NAME])

    def test_speaker_test_rejects_incompatible_wav(self) -> None:
        client = FakeSpeakerClient()
        with tempfile.TemporaryDirectory() as directory:
            audio_file = Path(directory) / "stereo.wav"
            with wave.open(str(audio_file), "wb") as recording:
                recording.setnchannels(2)
                recording.setsampwidth(2)
                recording.setframerate(44_100)
                recording.writeframes(b"\x00\x00" * 100)

            with self.assertRaisesRegex(ValueError, "mono PCM16"):
                play_speaker_test(audio_file, "eth0", client=client)

        self.assertEqual(client.blocks, [])

    def test_volume_change_sets_level_then_beeps(self) -> None:
        client = FakeAudioClient(20)

        result = set_robot_volume(
            40,
            "eth0",
            client=client,
            sleep=lambda _duration: None,
        )

        self.assertEqual(result, (20, 40))
        event_names = [event[0] for event in client.events]
        self.assertEqual(event_names[0:2], ["get", "set"])
        self.assertIn("play", event_names)
        self.assertEqual(event_names[-1], "get")
        self.assertEqual(client.stopped, [UNITREE_AUDIO_APP_NAME])

    def test_muting_beeps_before_setting_zero(self) -> None:
        client = FakeAudioClient(30)

        result = set_robot_volume(
            0,
            "eth0",
            client=client,
            sleep=lambda _duration: None,
        )

        self.assertEqual(result, (30, 0))
        event_names = [event[0] for event in client.events]
        self.assertLess(event_names.index("play"), event_names.index("set"))

    def test_robot_state_combines_low_state_battery_and_volume(self) -> None:
        motor = SimpleNamespace(
            mode=1,
            q=0.25,
            dq=-0.5,
            tau_est=1.5,
            temperature=[31, 32],
            vol=24.0,
            motorstate=7,
        )
        imu = SimpleNamespace(
            rpy=[0.1, 0.2, 0.3],
            gyroscope=[1.0, 2.0, 3.0],
            accelerometer=[4.0, 5.0, 6.0],
            temperature=35,
        )
        low_state = SimpleNamespace(
            tick=123,
            mode_pr=0,
            mode_machine=5,
            imu_state=imu,
            motor_state=[motor],
        )
        battery = SimpleNamespace(
            soc=78,
            soh=96,
            cell_vol=[3900, 3910, 0],
            bmsvoltage=[1, 2, 3],
            current=-120,
            temperature=[25, 26],
            cycle=42,
            bmsstate=[0, 0, 0, 0, 0],
        )
        low_reader = FakeStateReader(low_state)
        battery_reader = FakeStateReader(battery)

        state = read_robot_state(
            "eth0",
            timeout=1.5,
            low_state_reader=low_reader,
            battery_reader=battery_reader,
            audio_client=FakeAudioClient(55),
            fsm_client=FakeFsmClient(501),
        )

        self.assertEqual(state.volume, 55)
        self.assertEqual(state.fsm_id, 501)
        self.assertEqual(state.fsm_name, "firmware state 501")
        self.assertEqual(state.tick, 123)
        self.assertEqual(state.battery.soc, 78)
        self.assertEqual(state.battery.cell_voltages_mv, (3900, 3910))
        self.assertEqual(state.motors[0].temperatures, (31, 32))
        self.assertEqual(low_reader.timeouts, [1.5])
        self.assertEqual(battery_reader.timeouts, [1.5])

    def test_behavior_list_intersects_robot_and_official_actions(self) -> None:
        client = FakeBehaviorClient(
            {"actions": [{"id": 17, "name": "clap"}, {"id": 19, "name": "hug"}]}
        )

        behaviors, confirmed = list_robot_behaviors(
            "eth0",
            client=client,
        )

        self.assertTrue(confirmed)
        self.assertEqual(
            [(behavior.name, behavior.action_id) for behavior in behaviors],
            [("clap", 17), ("hug", 19)],
        )

    def test_held_behavior_releases_arms_after_execution(self) -> None:
        client = FakeBehaviorClient({"actions": [19, 99]})
        sleeps: list[float] = []

        behavior = execute_robot_behavior(
            "hug",
            "eth0",
            hold=1.5,
            client=client,
            fsm_client=FakeFsmClient(500),
            sleep=sleeps.append,
        )

        self.assertEqual(behavior.name, "hug")
        self.assertEqual(client.executed, [19, 99])
        self.assertEqual(sleeps, [1.5])

    def test_self_finishing_behavior_does_not_force_release(self) -> None:
        client = FakeBehaviorClient({"actions": [17]})

        behavior = execute_robot_behavior(
            "clap",
            "eth0",
            client=client,
            fsm_client=FakeFsmClient(501),
            sleep=lambda _duration: None,
        )

        self.assertEqual(behavior.name, "clap")
        self.assertEqual(client.executed, [17])

    def test_behavior_reports_incompatible_fsm_before_execution(self) -> None:
        client = FakeBehaviorClient({"actions": [19]})

        with self.assertRaisesRegex(RuntimeError, "cannot run in FSM 3"):
            execute_robot_behavior(
                "hug",
                "eth0",
                client=client,
                fsm_client=FakeFsmClient(3),
            )

        self.assertEqual(client.executed, [])

    def test_mode_get_returns_current_fsm_with_description(self) -> None:
        mode = get_robot_mode("eth0", fsm_client=FakeFsmClient(3))

        self.assertEqual(mode.name, "rest")
        self.assertEqual(mode.fsm_id, 3)
        self.assertIn("sitting", mode.description)

    def test_mode_list_contains_official_stable_modes(self) -> None:
        self.assertEqual(
            [(mode.name, mode.fsm_id) for mode in list_robot_modes()],
            [
                ("zero torque", 0),
                ("damp", 1),
                ("squat", 2),
                ("rest", 3),
                ("stand", 4),
                ("prepared", 500),
                ("firmware state 501", 501),
                ("lie to stand", 702),
                ("squat transition", 706),
                ("firmware state 801", 801),
            ],
        )

    def test_mode_set_prepared_runs_standup_sequence_from_zero_torque(self) -> None:
        fsm_client = FakeFsmClient(0)
        sleeps: list[float] = []

        previous, current = set_robot_mode(
            "prepared",
            "eth0",
            fsm_client=fsm_client,
            sleep=sleeps.append,
        )

        self.assertEqual((previous.fsm_id, current.fsm_id), (0, 500))
        self.assertEqual(fsm_client.requested, [1, 4, 500])
        self.assertEqual(sleeps, [2.0, 2.0, 8.0, 2.0])

    def test_mode_set_rest_releases_arms_then_enters_sit_fsm(self) -> None:
        behavior_client = FakeBehaviorClient({"actions": [99]})
        fsm_client = FakeFsmClient(500)
        sleeps: list[float] = []

        previous, current = set_robot_mode(
            "sit",
            "eth0",
            behavior_client=behavior_client,
            fsm_client=fsm_client,
            sleep=sleeps.append,
        )

        self.assertEqual((previous.fsm_id, current.fsm_id), (500, 3))
        self.assertEqual(behavior_client.executed, [99])
        self.assertEqual(fsm_client.requested, [3])
        self.assertEqual(sleeps, [2.0])

    def test_mode_resolves_names_aliases_and_ids(self) -> None:
        self.assertEqual(resolve_robot_mode("prepare").fsm_id, 500)
        self.assertEqual(resolve_robot_mode("zero-torque").fsm_id, 0)
        self.assertEqual(resolve_robot_mode("3").name, "rest")
        self.assertEqual(resolve_robot_mode("lie2standup").fsm_id, 702)
        with self.assertRaisesRegex(ValueError, "managed by the firmware"):
            resolve_robot_mode("501")
        with self.assertRaisesRegex(ValueError, "Unknown robot mode"):
            resolve_robot_mode("flying")

    def test_unknown_behavior_is_rejected_before_contacting_robot(self) -> None:
        client = FakeBehaviorClient({"actions": [19]})

        with self.assertRaisesRegex(ValueError, "Unknown behavior"):
            execute_robot_behavior(
                "dance",
                "eth0",
                client=client,
            )

        self.assertEqual(client.executed, [])

    def test_cli_uses_result_directory_by_default(self) -> None:
        self.assertEqual(
            RESULT_DIR,
            find_project_root() / "diagnostics" / "result",
        )
        args = build_parser().parse_args(
            ["microphone", "--duration", "5"]
        )
        photo = build_parser().parse_args(["photo"])
        video = build_parser().parse_args(["video", "--duration", "5"])
        speakers = build_parser().parse_args(["speakers"])
        cameras = build_parser().parse_args(["cameras"])
        state = build_parser().parse_args(["state"])
        volume = build_parser().parse_args(["volume", "65"])
        behavior_list = build_robot_parser().parse_args(["behavior", "list"])
        behavior_run = build_robot_parser().parse_args(
            ["behavior", "run", "high", "five", "--yes"]
        )
        mode_get = build_robot_parser().parse_args(["mode", "get"])
        mode_list = build_robot_parser().parse_args(["mode", "list"])
        mode_set = build_robot_parser().parse_args(
            ["mode", "set", "zero", "torque", "--yes"]
        )

        self.assertEqual(args.command, "microphone")
        self.assertEqual(args.duration, 5)
        self.assertEqual(args.output, DEFAULT_MICROPHONE_OUTPUT)
        self.assertEqual(photo.output, DEFAULT_PHOTO_OUTPUT)
        self.assertEqual(video.output, DEFAULT_VIDEO_OUTPUT)
        self.assertEqual(speakers.input, DEFAULT_MICROPHONE_OUTPUT)
        self.assertEqual(cameras.output_dir, RESULT_DIR)
        self.assertFalse(hasattr(photo, "camera"))
        self.assertFalse(hasattr(video, "camera"))
        self.assertFalse(hasattr(cameras, "color_camera"))
        self.assertEqual(state.timeout, 3.0)
        self.assertEqual(state.output, DEFAULT_STATE_OUTPUT)
        self.assertEqual(volume.level, 65)
        self.assertEqual(behavior_list.behavior_command, "list")
        self.assertEqual(behavior_run.name, ["high", "five"])
        self.assertTrue(behavior_run.yes)
        self.assertEqual(mode_get.mode_command, "get")
        self.assertEqual(mode_list.mode_command, "list")
        self.assertEqual(mode_set.name, ["zero", "torque"])
        self.assertTrue(mode_set.yes)


if __name__ == "__main__":
    unittest.main()
