"""Hardware services, state readers, and controls for the Unitree G1."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import math
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
from typing import Callable, Protocol
import wave


UNITREE_SAMPLE_RATE = 16_000
UNITREE_AUDIO_APP_NAME = "unitree-hardware-test"


class UnitreeSpeakerClient(Protocol):
    """Subset of the G1 AudioClient needed by the speaker diagnostic."""

    def PlayStream(self, app_name: str, stream_id: str, pcm_data: bytes) -> object: ...

    def PlayStop(self, app_name: str) -> object: ...


class UnitreeAudioClient(UnitreeSpeakerClient, Protocol):
    """Official G1 audio operations used by state and volume diagnostics."""

    def GetVolume(self) -> object: ...

    def SetVolume(self, volume: int) -> object: ...


class UnitreeVideoClient(Protocol):
    """Subset of the official VideoClient needed by camera diagnostics."""

    def GetImageSample(self) -> object: ...


class StateReader(Protocol):
    """Subset of ChannelSubscriber used to take one state sample."""

    def Read(self, timeout: float | None = None) -> object: ...


class UnitreeBehaviorClient(Protocol):
    """Official G1ArmActionClient operations used by behavior diagnostics."""

    def GetActionList(self) -> object: ...

    def ExecuteAction(self, action_id: int) -> object: ...


class UnitreeFsmClient(Protocol):
    """Official G1 LocoClient operations used for behavior preflight."""

    def GetFsmId(self) -> object: ...

    def SetFsmId(self, fsm_id: int) -> object: ...


class RobotBehaviorService:
    """Reuse the official arm-action clients throughout one application session."""

    def __init__(
        self,
        network_interface: str,
        *,
        initialize_channel: bool = True,
        client: UnitreeBehaviorClient | None = None,
        fsm_client: UnitreeFsmClient | None = None,
    ) -> None:
        if (client is None) != (fsm_client is None):
            raise ValueError("client and fsm_client must be provided together.")
        self._network_interface = network_interface
        self._initialize_channel = initialize_channel
        self._client = client
        self._fsm_client = fsm_client

    def _ensure_clients(
        self,
    ) -> tuple[UnitreeBehaviorClient, UnitreeFsmClient]:
        if self._client is None or self._fsm_client is None:
            self._client = _create_behavior_client(
                self._network_interface,
                initialize_channel=self._initialize_channel,
            )
            self._fsm_client = _create_fsm_client()
        return self._client, self._fsm_client

    def list(self) -> tuple[tuple[RobotBehavior, ...], bool]:
        """List behaviors reported by the G1 using the shared client."""
        client, _fsm_client = self._ensure_clients()
        return list_robot_behaviors(self._network_interface, client=client)

    def execute(self, name: str, *, hold: float = 2.0) -> RobotBehavior:
        """Execute a behavior using the shared arm-action and FSM clients."""
        client, fsm_client = self._ensure_clients()
        return execute_robot_behavior(
            name,
            self._network_interface,
            hold=hold,
            client=client,
            fsm_client=fsm_client,
        )


@dataclass(frozen=True)
class RobotBehavior:
    name: str
    action_id: int
    release_after: bool = False


@dataclass(frozen=True)
class RobotMode:
    name: str
    fsm_id: int
    description: str
    settable: bool = True


ROBOT_BEHAVIORS = (
    RobotBehavior("release arm", 99),
    RobotBehavior("two-hand kiss", 11),
    RobotBehavior("left kiss", 12),
    RobotBehavior("right kiss", 13),
    RobotBehavior("hands up", 15, True),
    RobotBehavior("clap", 17),
    RobotBehavior("high five", 18, True),
    RobotBehavior("hug", 19, True),
    RobotBehavior("heart", 20, True),
    RobotBehavior("right heart", 21, True),
    RobotBehavior("reject", 22, True),
    RobotBehavior("right hand up", 23, True),
    RobotBehavior("x-ray", 24, True),
    RobotBehavior("face wave", 25),
    RobotBehavior("high wave", 26),
    RobotBehavior("shake hand", 27, True),
)
RELEASE_ARM_ACTION_ID = 99
BEHAVIOR_FSM_IDS = {500, 501, 801}
ROBOT_MODES = (
    RobotMode(
        "zero torque",
        0,
        "Motor torque is disabled; the robot must be physically supported.",
    ),
    RobotMode(
        "damp",
        1,
        "Motor damping is enabled; the robot may no longer support its posture.",
    ),
    RobotMode("squat", 2, "Official squatting posture."),
    RobotMode("rest", 3, "Official sitting/rest state."),
    RobotMode(
        "stand",
        4,
        "Official stand-up transition; the robot rises and locks its posture.",
    ),
    RobotMode(
        "prepared",
        500,
        "Official active state for arm behaviors; entering it may stand the robot.",
    ),
    RobotMode(
        "firmware state 501",
        501,
        "Undocumented firmware-managed state accepted by the arm-action service.",
        False,
    ),
    RobotMode(
        "lie to stand",
        702,
        "Official transition for standing up from a lying position.",
    ),
    RobotMode(
        "squat transition",
        706,
        "Official squat-to-stand/stand-to-squat transition; "
        "direction depends on posture.",
    ),
    RobotMode(
        "firmware state 801",
        801,
        "Undocumented arm-action state; actions require FSM mode 0 or 3.",
        False,
    ),
)


@dataclass(frozen=True)
class MotorSnapshot:
    index: int
    mode: int
    position: float
    velocity: float
    torque: float
    temperatures: tuple[int, ...]
    voltage: float
    state: int


@dataclass(frozen=True)
class BatterySnapshot:
    soc: int
    soh: int
    cell_voltages_mv: tuple[int, ...]
    pack_voltages_raw: tuple[int, ...]
    current_raw: int
    temperatures_raw: tuple[int, ...]
    cycles: int
    state: tuple[int, ...]


@dataclass(frozen=True)
class RobotStateSnapshot:
    volume: int
    fsm_id: int
    fsm_name: str
    tick: int
    mode_pr: int
    mode_machine: int
    imu_rpy: tuple[float, ...]
    imu_gyroscope: tuple[float, ...]
    imu_accelerometer: tuple[float, ...]
    imu_temperature: int
    motors: tuple[MotorSnapshot, ...]
    battery: BatterySnapshot | None


def require_positive(value: float, name: str) -> None:
    """Raise a useful error when a numeric argument is not positive."""
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")


def camera_device(value: str) -> int | str:
    """Convert a numeric camera selector while preserving device paths."""
    stripped = value.strip()
    try:
        return int(stripped)
    except ValueError:
        return stripped


def record_microphone(
    duration: float,
    output: Path,
    interface_ip: str,
    multicast_group: str,
    port: int,
) -> int:
    """Record the G1 microphone multicast stream as mono PCM16 WAV."""
    require_positive(duration, "duration")
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except OSError:
        pass
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_MULTICAST_IF,
        socket.inet_aton(interface_ip),
    )
    sock.bind(("", port))
    membership = struct.pack(
        "=4s4s",
        socket.inet_aton(multicast_group),
        socket.inet_aton(interface_ip),
    )
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    sock.settimeout(0.5)

    temporary_name: str | None = None
    bytes_written = 0
    target_bytes = round(duration * UNITREE_SAMPLE_RATE) * 2
    last_packet_at = time.monotonic()
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
        with wave.open(temporary_name, "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(UNITREE_SAMPLE_RATE)
            while bytes_written < target_bytes:
                try:
                    data, _source = sock.recvfrom(65_536)
                except socket.timeout:
                    if time.monotonic() - last_packet_at >= 5.0:
                        raise RuntimeError(
                            "The G1 microphone stopped sending audio. Enable "
                            "Voice Assistant > Wake-up Conversation Mode and "
                            "verify the multicast settings."
                        )
                    continue
                last_packet_at = time.monotonic()
                if len(data) % 2:
                    data = data[:-1]
                if data:
                    data = data[: target_bytes - bytes_written]
                    recording.writeframesraw(data)
                    bytes_written += len(data)
        Path(temporary_name).replace(output)
        temporary_name = None
        return bytes_written // 2
    finally:
        sock.close()
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _capture_v4l2_frame(
    device: int | str,
    pixel_format: str,
    width: int,
    height: int,
    bytes_per_pixel: int,
) -> bytes:
    """Capture one raw V4L2 frame with the command verified on the G1."""
    device_path = f"/dev/video{device}" if isinstance(device, int) else device
    expected_size = width * height * bytes_per_pixel
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".unitree-camera-",
            suffix=".raw",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name

        command = [
            "v4l2-ctl",
            f"--device={device_path}",
            (
                f"--set-fmt-video=width={width},height={height},"
                f"pixelformat={pixel_format}"
            ),
            "--stream-mmap",
            "--stream-count=1",
            f"--stream-to={temporary_name}",
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                "v4l2-ctl is not installed. Install the v4l-utils package."
            ) from error
        if result.returncode != 0:
            detail = result.stdout.strip() or "unknown v4l2-ctl error"
            raise RuntimeError(
                f"Could not capture {device_path} as {pixel_format}: {detail}"
            )

        frame = Path(temporary_name).read_bytes()
        if len(frame) != expected_size:
            raise RuntimeError(
                f"Camera {device_path} returned {len(frame)} bytes; "
                f"expected {expected_size} for {width}x{height}."
            )
        return frame
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _create_video_client(network_interface: str) -> UnitreeVideoClient:
    """Create the camera client in the same way as the official SDK example."""
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.go2.video.video_client import VideoClient
    except ImportError as error:
        raise RuntimeError(
            "The Unitree SDK is not installed. "
            "Run ./scripts/install-unitree.sh first."
        ) from error

    ChannelFactoryInitialize(0, network_interface)
    client = VideoClient()
    client.SetTimeout(3.0)
    client.Init()
    return client


def _read_unitree_video_frame(client: UnitreeVideoClient) -> tuple[object, object]:
    """Request and decode one JPEG frame using VideoClient.GetImageSample."""
    try:
        import cv2
        import numpy as np
    except ImportError as error:
        raise RuntimeError(
            "OpenCV and NumPy are required. "
            "Run ./scripts/install-unitree.sh again."
        ) from error

    response = client.GetImageSample()
    if not isinstance(response, tuple) or len(response) != 2:
        raise RuntimeError(
            "The Unitree VideoClient returned an unexpected response."
        )
    status_code, encoded_image = response
    if status_code != 0:
        raise RuntimeError(
            f"The Unitree VideoClient returned status code {status_code}."
        )

    encoded = np.frombuffer(bytes(encoded_image), dtype=np.uint8)
    frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("The Unitree VideoClient returned an invalid image.")
    return cv2, frame


def take_photo(
    output: Path,
    network_interface: str,
    *,
    client: UnitreeVideoClient | None = None,
) -> tuple[int, int]:
    """Capture one colour frame through the official Unitree VideoClient."""
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    video_client = client or _create_video_client(network_interface)
    cv2, frame = _read_unitree_video_frame(video_client)
    if not cv2.imwrite(str(output), frame):
        raise RuntimeError(
            f"Could not encode the photo. Check the extension of {output.name!r}."
        )
    height, width = frame.shape[:2]
    return width, height


def capture_realsense_outputs(
    output_dir: Path,
    network_interface: str,
    depth_device: int | str,
    infrared_device: int | str,
    *,
    video_client: UnitreeVideoClient | None = None,
) -> list[Path]:
    """Capture Unitree colour plus local depth and stereo infrared images."""
    try:
        import cv2
        import numpy as np
    except ImportError as error:
        raise RuntimeError(
            "OpenCV and NumPy are required. "
            "Run ./scripts/install-unitree.sh again."
        ) from error

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []

    color_output = output_dir / "camera-color.jpg"
    take_photo(
        color_output,
        network_interface,
        client=video_client,
    )
    outputs.append(color_output)

    depth_raw = _capture_v4l2_frame(
        depth_device,
        pixel_format="0x2036315a",
        width=640,
        height=480,
        bytes_per_pixel=2,
    )
    depth = np.frombuffer(depth_raw, dtype="<u2").reshape(480, 640)

    raw_depth_output = output_dir / "camera-depth-raw.png"
    if not cv2.imwrite(str(raw_depth_output), depth):
        raise RuntimeError("Could not save the raw depth image.")
    outputs.append(raw_depth_output)

    valid_depth = depth[depth > 0]
    if valid_depth.size:
        near = float(np.percentile(valid_depth, 2))
        far = float(np.percentile(valid_depth, 98))
        if far <= near:
            far = near + 1.0
        visible_depth = np.clip((depth - near) * 255.0 / (far - near), 0, 255)
        visible_depth = visible_depth.astype(np.uint8)
        visible_depth[depth == 0] = 0
    else:
        visible_depth = np.zeros(depth.shape, dtype=np.uint8)
    colour_depth = cv2.applyColorMap(
        visible_depth,
        cv2.COLORMAP_TURBO,
    )
    depth_output = output_dir / "camera-depth.jpg"
    if not cv2.imwrite(str(depth_output), colour_depth):
        raise RuntimeError("Could not save the depth visualization.")
    outputs.append(depth_output)

    infrared_raw = _capture_v4l2_frame(
        infrared_device,
        pixel_format="0x20493859",
        width=640,
        height=480,
        bytes_per_pixel=2,
    )
    infrared = np.frombuffer(infrared_raw, dtype=np.uint8).reshape(480, 640, 2)
    infrared_left = infrared[:, :, 0]
    infrared_right = infrared[:, :, 1]
    for name, image in (
        ("camera-infrared-left.png", infrared_left),
        ("camera-infrared-right.png", infrared_right),
    ):
        output = output_dir / name
        if not cv2.imwrite(str(output), image):
            raise RuntimeError(f"Could not save {name}.")
        outputs.append(output)

    return outputs


def record_video(
    duration: float,
    output: Path,
    network_interface: str,
    fps: float = 20.0,
    *,
    client: UnitreeVideoClient | None = None,
) -> tuple[int, int, int]:
    """Record the official Unitree colour stream to MP4 or AVI."""
    require_positive(duration, "duration")
    require_positive(fps, "fps")
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    video_client = client or _create_video_client(network_interface)
    writer = None
    try:
        cv2, frame = _read_unitree_video_frame(video_client)
        height, width = frame.shape[:2]
        codec = "MJPG" if output.suffix.lower() == ".avi" else "mp4v"
        writer = cv2.VideoWriter(
            str(output),
            cv2.VideoWriter_fourcc(*codec),
            fps,
            (width, height),
        )
        if not writer.isOpened():
            raise RuntimeError(
                f"Could not create video {output}. Use an .mp4 or .avi extension."
            )

        frames = 0
        started_at = time.monotonic()
        deadline = started_at + duration
        while time.monotonic() < deadline:
            if frames:
                _cv2, frame = _read_unitree_video_frame(video_client)
                next_height, next_width = frame.shape[:2]
                if (next_width, next_height) != (width, height):
                    raise RuntimeError(
                        "The Unitree camera changed resolution while recording."
                    )
            writer.write(frame)
            frames += 1
            delay = started_at + (frames / fps) - time.monotonic()
            if delay > 0:
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        return frames, width, height
    finally:
        if writer is not None:
            writer.release()


def _create_audio_client(network_interface: str) -> UnitreeAudioClient:
    """Create the official G1 AudioClient."""
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
    except ImportError as error:
        raise RuntimeError(
            "The Unitree SDK is not installed. "
            "Run ./scripts/install-unitree.sh first."
        ) from error

    ChannelFactoryInitialize(0, network_interface)
    client = AudioClient()
    client.SetTimeout(3.0)
    client.Init()
    return client


def _response_code(response: object) -> object:
    return response[0] if isinstance(response, tuple) else response


def _get_volume(client: UnitreeAudioClient) -> int:
    response = client.GetVolume()
    if not isinstance(response, tuple) or len(response) != 2:
        raise RuntimeError("The G1 AudioClient returned an unexpected volume response.")
    code, data = response
    if code != 0:
        raise RuntimeError(f"The G1 AudioClient returned status code {code}.")
    if isinstance(data, dict):
        data = data.get("volume", data.get("level"))
    if isinstance(data, bool) or not isinstance(data, (int, float)):
        raise RuntimeError(
            f"The G1 AudioClient returned invalid volume data: {data!r}."
        )
    return int(data)


def _play_pcm(
    client: UnitreeSpeakerClient,
    pcm: bytes,
    *,
    stream_id: str,
    sleep: Callable[[float], None],
) -> int:
    chunk_size = UNITREE_SAMPLE_RATE // 10 * 2
    bytes_sent = 0
    try:
        for offset in range(0, len(pcm), chunk_size):
            block = pcm[offset : offset + chunk_size]
            result = client.PlayStream(UNITREE_AUDIO_APP_NAME, stream_id, block)
            status_code = _response_code(result)
            if status_code not in (None, 0):
                raise RuntimeError(
                    f"The G1 AudioClient returned status code {status_code}."
                )
            bytes_sent += len(block)
            sleep(len(block) / (UNITREE_SAMPLE_RATE * 2))
        return bytes_sent // 2
    finally:
        try:
            client.PlayStop(UNITREE_AUDIO_APP_NAME)
        except Exception:
            pass


def _beep_pcm(
    frequency: float = 880.0,
    duration: float = 0.18,
    amplitude: float = 0.25,
) -> bytes:
    """Create a short PCM16 beep with a click-preventing fade."""
    sample_count = round(duration * UNITREE_SAMPLE_RATE)
    fade_samples = max(1, round(0.015 * UNITREE_SAMPLE_RATE))
    samples = array("h")
    for index in range(sample_count):
        envelope = min(
            1.0,
            index / fade_samples,
            (sample_count - index - 1) / fade_samples,
        )
        value = math.sin(2.0 * math.pi * frequency * index / UNITREE_SAMPLE_RATE)
        samples.append(round(32_767 * amplitude * envelope * value))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def set_robot_volume(
    volume: int,
    network_interface: str,
    *,
    client: UnitreeAudioClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, int]:
    """Set G1 volume and play a confirmation beep when it changes."""
    if isinstance(volume, bool) or not 0 <= volume <= 100:
        raise ValueError("volume must be between 0 and 100.")

    audio_client = client or _create_audio_client(network_interface)
    previous = _get_volume(audio_client)
    if previous == volume:
        return previous, volume

    beep = _beep_pcm()
    if volume == 0:
        _play_pcm(
            audio_client,
            beep,
            stream_id=f"volume-beep-{time.monotonic_ns()}",
            sleep=sleep,
        )

    result = audio_client.SetVolume(volume)
    status_code = _response_code(result)
    if status_code not in (None, 0):
        raise RuntimeError(
            f"The G1 AudioClient returned status code {status_code} "
            "while setting the volume."
        )

    if volume > 0:
        sleep(0.1)
        _play_pcm(
            audio_client,
            beep,
            stream_id=f"volume-beep-{time.monotonic_ns()}",
            sleep=sleep,
        )

    confirmed = _get_volume(audio_client)
    if confirmed != volume:
        raise RuntimeError(
            f"The G1 reported volume {confirmed} after setting it to {volume}."
        )
    return previous, confirmed


def _create_state_readers(
    network_interface: str,
) -> tuple[StateReader, StateReader, UnitreeAudioClient, UnitreeFsmClient]:
    """Create the read-only subscribers and audio client from official SDK types."""
    try:
        from unitree_sdk2py.core.channel import (
            ChannelFactoryInitialize,
            ChannelSubscriber,
        )
        from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import BmsState_, LowState_
    except ImportError as error:
        raise RuntimeError(
            "The Unitree SDK is not installed. "
            "Run ./scripts/install-unitree.sh first."
        ) from error

    ChannelFactoryInitialize(0, network_interface)
    low_state_reader = ChannelSubscriber("rt/lowstate", LowState_)
    low_state_reader.Init()
    battery_reader = ChannelSubscriber("rt/lf/bmsstate", BmsState_)
    battery_reader.Init()
    audio_client = AudioClient()
    audio_client.SetTimeout(3.0)
    audio_client.Init()
    fsm_client = _create_fsm_client()
    return low_state_reader, battery_reader, audio_client, fsm_client


def read_robot_state(
    network_interface: str,
    *,
    timeout: float = 3.0,
    low_state_reader: StateReader | None = None,
    battery_reader: StateReader | None = None,
    audio_client: UnitreeAudioClient | None = None,
    fsm_client: UnitreeFsmClient | None = None,
) -> RobotStateSnapshot:
    """Read a safe snapshot of G1 low-level, battery, and audio state."""
    require_positive(timeout, "timeout")
    if (
        low_state_reader is None
        or battery_reader is None
        or audio_client is None
        or fsm_client is None
    ):
        if any(
            item is not None
            for item in (
                low_state_reader,
                battery_reader,
                audio_client,
                fsm_client,
            )
        ):
            raise ValueError(
                "low_state_reader, battery_reader, audio_client, and fsm_client "
                "must be provided together."
            )
        (
            low_state_reader,
            battery_reader,
            audio_client,
            fsm_client,
        ) = _create_state_readers(network_interface)

    low_state = low_state_reader.Read(timeout)
    if low_state is None:
        raise RuntimeError(
            f"No G1 low-level state received from rt/lowstate within {timeout:g}s."
        )
    battery_state = battery_reader.Read(timeout)

    imu = low_state.imu_state
    motors = tuple(
        MotorSnapshot(
            index=index,
            mode=int(motor.mode),
            position=float(motor.q),
            velocity=float(motor.dq),
            torque=float(motor.tau_est),
            temperatures=tuple(int(value) for value in motor.temperature),
            voltage=float(motor.vol),
            state=int(motor.motorstate),
        )
        for index, motor in enumerate(low_state.motor_state)
    )
    battery = None
    if battery_state is not None:
        battery = BatterySnapshot(
            soc=int(battery_state.soc),
            soh=int(battery_state.soh),
            cell_voltages_mv=tuple(
                int(value) for value in battery_state.cell_vol if value
            ),
            pack_voltages_raw=tuple(
                int(value) for value in battery_state.bmsvoltage
            ),
            current_raw=int(battery_state.current),
            temperatures_raw=tuple(
                int(value) for value in battery_state.temperature
            ),
            cycles=int(battery_state.cycle),
            state=tuple(int(value) for value in battery_state.bmsstate),
        )

    fsm_id = _get_fsm_id(fsm_client)
    fsm_mode = _mode_for_fsm(fsm_id)
    return RobotStateSnapshot(
        volume=_get_volume(audio_client),
        fsm_id=fsm_id,
        fsm_name=fsm_mode.name,
        tick=int(low_state.tick),
        mode_pr=int(low_state.mode_pr),
        mode_machine=int(low_state.mode_machine),
        imu_rpy=tuple(float(value) for value in imu.rpy),
        imu_gyroscope=tuple(float(value) for value in imu.gyroscope),
        imu_accelerometer=tuple(float(value) for value in imu.accelerometer),
        imu_temperature=int(imu.temperature),
        motors=motors,
        battery=battery,
    )


def _create_behavior_client(
    network_interface: str,
    *,
    initialize_channel: bool = True,
) -> UnitreeBehaviorClient:
    """Create the official G1 arm-action client."""
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient
    except ImportError as error:
        raise RuntimeError(
            "The installed Unitree SDK does not provide G1ArmActionClient. "
            "Run ./scripts/install-unitree.sh to update it."
        ) from error

    if initialize_channel:
        ChannelFactoryInitialize(0, network_interface)
    client = G1ArmActionClient()
    client.SetTimeout(10.0)
    client.Init()
    return client


def _create_fsm_client() -> UnitreeFsmClient:
    """Create LocoClient after the shared DDS channel has been initialized."""
    try:
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
    except ImportError as error:
        raise RuntimeError(
            "The installed Unitree SDK does not provide the G1 LocoClient."
        ) from error

    client = LocoClient()
    client.SetTimeout(10.0)
    client.Init()
    return client


def _known_behavior_ids(value: object) -> set[int]:
    """Extract whitelisted action IDs from varying firmware response shapes."""
    official_ids = {behavior.action_id for behavior in ROBOT_BEHAVIORS}
    official_names = {
        " ".join(
            behavior.name.lower().replace("-", " ").replace("_", " ").split()
        ): behavior.action_id
        for behavior in ROBOT_BEHAVIORS
    }
    found: set[int] = set()

    def visit(item: object) -> None:
        if isinstance(item, bool):
            return
        if isinstance(item, int):
            if item in official_ids:
                found.add(item)
            return
        if isinstance(item, str):
            normalized = " ".join(
                item.lower().replace("-", " ").replace("_", " ").split()
            )
            if normalized in official_names:
                found.add(official_names[normalized])
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                visit(key)
                visit(nested)
            return
        if isinstance(item, (list, tuple, set)):
            for nested in item:
                visit(nested)

    visit(value)
    return found


def list_robot_behaviors(
    network_interface: str,
    *,
    client: UnitreeBehaviorClient | None = None,
) -> tuple[tuple[RobotBehavior, ...], bool]:
    """Ask the robot for its actions and intersect them with the official map."""
    behavior_client = client or _create_behavior_client(network_interface)
    response = behavior_client.GetActionList()
    if not isinstance(response, tuple) or len(response) != 2:
        raise RuntimeError(
            "The G1 arm-action service returned an unexpected action-list response."
        )
    code, data = response
    if code != 0:
        raise RuntimeError(
            f"The G1 arm-action service returned status code {code} "
            "while listing behaviors."
        )

    reported_ids = _known_behavior_ids(data)
    if not reported_ids:
        return ROBOT_BEHAVIORS, False
    return (
        tuple(
            behavior
            for behavior in ROBOT_BEHAVIORS
            if behavior.action_id in reported_ids
        ),
        True,
    )


def _normalize_behavior_name(value: str) -> str:
    return " ".join(value.lower().replace("-", " ").replace("_", " ").split())


def _get_fsm_id(client: UnitreeFsmClient) -> int:
    response = client.GetFsmId()
    if not isinstance(response, tuple) or len(response) != 2:
        raise RuntimeError("The G1 LocoClient returned an unexpected FSM response.")
    code, fsm_id = response
    if code != 0:
        raise RuntimeError(
            f"The G1 LocoClient returned status code {code} while reading FSM."
        )
    if isinstance(fsm_id, bool) or not isinstance(fsm_id, (int, float)):
        raise RuntimeError(f"The G1 returned an invalid FSM ID: {fsm_id!r}.")
    return int(fsm_id)


def _mode_for_fsm(fsm_id: int) -> RobotMode:
    return next(
        (mode for mode in ROBOT_MODES if mode.fsm_id == fsm_id),
        RobotMode(
            "unknown",
            fsm_id,
            "FSM not described by this diagnostic.",
            False,
        ),
    )


def list_robot_modes() -> tuple[RobotMode, ...]:
    """Return the official stable modes exposed by the G1 LocoClient."""
    return ROBOT_MODES


def resolve_robot_mode(value: str | int) -> RobotMode:
    """Resolve a mode by its canonical name, official alias, or FSM ID."""
    normalized = _normalize_behavior_name(str(value))
    aliases = {
        "start": "prepared",
        "prepare": "prepared",
        "sit": "rest",
        "stand up": "stand",
        "standup": "stand",
        "lie2standup": "lie to stand",
        "lie to stand up": "lie to stand",
        "squat2standup": "squat transition",
        "zero": "zero torque",
        "zero torque": "zero torque",
    }
    normalized = aliases.get(normalized, normalized)
    mode = next(
        (
            candidate
            for candidate in ROBOT_MODES
            if candidate.name == normalized or str(candidate.fsm_id) == normalized
        ),
        None,
    )
    if mode is None:
        choices = ", ".join(
            f"{candidate.name} ({candidate.fsm_id})"
            for candidate in ROBOT_MODES
        )
        raise ValueError(f"Unknown robot mode {value!r}. Available modes: {choices}.")
    if not mode.settable:
        raise ValueError(
            f"Robot mode {mode.name!r} (FSM {mode.fsm_id}) is managed by "
            "the firmware and cannot be requested directly."
        )
    return mode


def get_robot_mode(
    network_interface: str,
    *,
    fsm_client: UnitreeFsmClient | None = None,
) -> RobotMode:
    """Read the current FSM and attach a known human-readable description."""
    if fsm_client is None:
        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        except ImportError as error:
            raise RuntimeError(
                "The Unitree SDK is not installed. "
                "Run ./scripts/install-unitree.sh first."
            ) from error
        ChannelFactoryInitialize(0, network_interface)
        fsm_client = _create_fsm_client()
    return _mode_for_fsm(_get_fsm_id(fsm_client))


def _set_and_confirm_fsm(
    client: UnitreeFsmClient,
    target_fsm_id: int,
    *,
    sleep: Callable[[float], None],
) -> tuple[int, int]:
    previous = _get_fsm_id(client)
    if previous == target_fsm_id:
        return previous, previous
    result = client.SetFsmId(target_fsm_id)
    status_code = _response_code(result)
    if status_code not in (None, 0):
        raise RuntimeError(
            f"The G1 failed to enter FSM {target_fsm_id} "
            f"(status code {status_code})."
        )
    sleep(2.0)
    current = _get_fsm_id(client)
    if current != target_fsm_id:
        raise RuntimeError(
            f"The G1 reported FSM {current} after requesting FSM {target_fsm_id}."
        )
    return previous, current


def set_robot_mode(
    mode: str | int,
    network_interface: str,
    *,
    behavior_client: UnitreeBehaviorClient | None = None,
    fsm_client: UnitreeFsmClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[RobotMode, RobotMode]:
    """Set one whitelisted official G1 mode and confirm the resulting FSM."""
    target = resolve_robot_mode(mode)
    if fsm_client is None:
        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        except ImportError as error:
            raise RuntimeError(
                "The Unitree SDK is not installed. "
                "Run ./scripts/install-unitree.sh first."
            ) from error
        ChannelFactoryInitialize(0, network_interface)
        fsm_client = _create_fsm_client()

    previous = _get_fsm_id(fsm_client)
    if previous in BEHAVIOR_FSM_IDS and previous != target.fsm_id:
        if behavior_client is None:
            behavior_client = _create_behavior_client(
                network_interface,
                initialize_channel=False,
            )
        release_result = behavior_client.ExecuteAction(RELEASE_ARM_ACTION_ID)
        release_code = _response_code(release_result)
        if release_code not in (None, 0):
            release_action = next(
                item
                for item in ROBOT_BEHAVIORS
                if item.action_id == RELEASE_ARM_ACTION_ID
            )
            raise _behavior_error(release_action, release_code)

    # Start/FSM 500 is not accepted directly from passive or sitting states.
    # Follow the G1 LocoClient's official posture states: Damp -> StandUp -> Start.
    if target.fsm_id == 500 and previous in {0, 1, 3, 4}:
        transition_from = previous
        if transition_from == 0:
            _set_and_confirm_fsm(fsm_client, 1, sleep=sleep)
            transition_from = 1
        if transition_from in {1, 3}:
            _set_and_confirm_fsm(fsm_client, 4, sleep=sleep)
            transition_from = 4
        if transition_from == 4:
            # FSM 4 is reported before the physical stand-up motion finishes.
            sleep(8.0)

    _previous, current = _set_and_confirm_fsm(
        fsm_client,
        target.fsm_id,
        sleep=sleep,
    )
    return _mode_for_fsm(previous), _mode_for_fsm(current)


def _behavior_error(action: RobotBehavior, status_code: object) -> RuntimeError:
    descriptions = {
        7400: "rt/armsdk is occupied by another controller",
        7401: "the arms are holding a previous pose; run 'release arm' first",
        7402: "the firmware rejected the action ID",
        7404: (
            "the current FSM is incompatible; arm actions require FSM "
            "500, 501, or 801 (FSM 801 additionally requires mode 0 or 3)"
        ),
    }
    detail = descriptions.get(status_code, "unknown arm-action service error")
    return RuntimeError(
        f"The G1 arm-action service returned status code {status_code} "
        f"for behavior {action.name!r}: {detail}."
    )


def execute_robot_behavior(
    name: str,
    network_interface: str,
    *,
    hold: float = 2.0,
    client: UnitreeBehaviorClient | None = None,
    fsm_client: UnitreeFsmClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RobotBehavior:
    """Execute one whitelisted official arm behavior and safely release its pose."""
    require_positive(hold, "hold")
    normalized_name = _normalize_behavior_name(name)
    behavior = next(
        (
            candidate
            for candidate in ROBOT_BEHAVIORS
            if _normalize_behavior_name(candidate.name) == normalized_name
        ),
        None,
    )
    if behavior is None:
        choices = ", ".join(item.name for item in ROBOT_BEHAVIORS)
        raise ValueError(f"Unknown behavior {name!r}. Available behaviors: {choices}.")

    if client is None and fsm_client is None:
        behavior_client = _create_behavior_client(network_interface)
        loco_client = _create_fsm_client()
    elif client is not None and fsm_client is not None:
        behavior_client = client
        loco_client = fsm_client
    else:
        raise ValueError("client and fsm_client must be provided together.")

    available, confirmed_by_robot = list_robot_behaviors(
        network_interface,
        client=behavior_client,
    )
    if confirmed_by_robot and behavior not in available:
        raise RuntimeError(
            f"Behavior {behavior.name!r} is not reported by this robot's firmware."
        )

    if behavior.action_id != RELEASE_ARM_ACTION_ID:
        fsm_id = _get_fsm_id(loco_client)
        if fsm_id not in BEHAVIOR_FSM_IDS:
            raise RuntimeError(
                f"Behavior {behavior.name!r} cannot run in FSM {fsm_id}. "
                "Arm actions require FSM 500, 501, or 801. Run "
                "'robot mode set prepared' first."
            )

    result = behavior_client.ExecuteAction(behavior.action_id)
    status_code = _response_code(result)
    if status_code not in (None, 0):
        raise _behavior_error(behavior, status_code)

    if behavior.release_after:
        try:
            sleep(hold)
        finally:
            release_result = behavior_client.ExecuteAction(RELEASE_ARM_ACTION_ID)
            release_code = _response_code(release_result)
            if release_code not in (None, 0):
                error = _behavior_error(
                    next(
                        item
                        for item in ROBOT_BEHAVIORS
                        if item.action_id == RELEASE_ARM_ACTION_ID
                    ),
                    release_code,
                )
                raise RuntimeError(
                    f"The behavior ran, but the G1 failed to release its arms: {error}"
                )
    return behavior


def test_speakers(
    input_file: Path,
    network_interface: str,
    *,
    client: UnitreeSpeakerClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Play a mono PCM16/16 kHz WAV file through the G1 speaker."""
    input_file = input_file.expanduser().resolve()
    if not input_file.is_file():
        raise RuntimeError(
            f"Audio file not found: {input_file}. Run the microphone test first "
            "or provide --input."
        )
    try:
        with wave.open(str(input_file), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != UNITREE_SAMPLE_RATE
                or source.getcomptype() != "NONE"
            ):
                raise ValueError(
                    "The speaker input must be an uncompressed mono PCM16 WAV "
                    f"at {UNITREE_SAMPLE_RATE} Hz."
                )
            pcm = source.readframes(source.getnframes())
    except wave.Error as error:
        raise ValueError(f"Invalid WAV file: {input_file}") from error
    if not pcm:
        raise ValueError(f"The WAV file contains no audio: {input_file}")

    speaker_client = client or _create_audio_client(network_interface)
    return _play_pcm(
        speaker_client,
        pcm,
        stream_id=f"wav-{time.monotonic_ns()}",
        sleep=sleep,
    )
