"""Command-line interface for Unitree G1 hardware diagnostics."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from my_unitree.configuration import (
    find_project_root,
    load_project_configuration,
)
from robot.services import (
    RobotStateSnapshot,
    capture_realsense_outputs,
    camera_device,
    read_robot_state,
    record_microphone,
    record_video,
    set_robot_volume,
    take_photo,
    test_speakers,
)


PROJECT_ROOT = find_project_root()
RESULT_DIR = PROJECT_ROOT / "diagnostics" / "result"
DEFAULT_MICROPHONE_OUTPUT = RESULT_DIR / "microphone.wav"
DEFAULT_PHOTO_OUTPUT = RESULT_DIR / "photo.jpg"
DEFAULT_VIDEO_OUTPUT = RESULT_DIR / "video.mp4"
DEFAULT_STATE_OUTPUT = RESULT_DIR / "robot-state.json"


def build_parser() -> argparse.ArgumentParser:
    """Build the diagnostics command parser."""
    parser = argparse.ArgumentParser(
        prog="diagnostics",
        description="Test the Unitree G1 microphone, camera, video, and speakers.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    command_format = argparse.ArgumentDefaultsHelpFormatter

    microphone = commands.add_parser(
        "microphone",
        help="record the G1 microphone array to WAV",
        formatter_class=command_format,
    )
    microphone.add_argument(
        "--duration", type=float, required=True, help="recording time in seconds"
    )
    microphone.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_MICROPHONE_OUTPUT,
        help="destination WAV file",
    )

    photo = commands.add_parser(
        "photo",
        help="capture one camera image",
        formatter_class=command_format,
    )
    photo.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_PHOTO_OUTPUT,
        help="destination image file",
    )

    video = commands.add_parser(
        "video",
        help="record the camera to MP4 or AVI",
        formatter_class=command_format,
    )
    video.add_argument(
        "--duration", type=float, required=True, help="recording time in seconds"
    )
    video.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_VIDEO_OUTPUT,
        help="destination MP4 or AVI file",
    )
    video.add_argument("--fps", type=float, default=20.0, help="output frame rate")

    cameras = commands.add_parser(
        "cameras",
        help="capture every RealSense image stream",
        formatter_class=command_format,
    )
    cameras.add_argument(
        "--output-dir",
        type=Path,
        default=RESULT_DIR,
        help="directory for colour, depth, and infrared images",
    )
    cameras.add_argument("--depth-camera", default=None)
    cameras.add_argument("--infrared-camera", default=None)

    speakers = commands.add_parser(
        "speakers",
        help="play a WAV file through the G1",
        formatter_class=command_format,
    )
    speakers.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_MICROPHONE_OUTPUT,
        help="mono PCM16/16 kHz WAV file",
    )

    state = commands.add_parser(
        "state",
        help="read battery, IMU, motor, control, and audio state",
        formatter_class=command_format,
    )
    state.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="seconds to wait for each DDS state stream",
    )
    state.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_STATE_OUTPUT,
        help="destination JSON snapshot",
    )

    volume = commands.add_parser(
        "volume",
        help="set G1 speaker volume and play a confirmation beep",
        formatter_class=command_format,
    )
    volume.add_argument("level", type=int, help="volume level from 0 to 100")

    return parser


def _format_vector(values: tuple[float, ...]) -> str:
    return "[" + ", ".join(f"{value:.3f}" for value in values) + "]"


def print_robot_state(state: RobotStateSnapshot) -> None:
    """Print a compact but useful G1 state report."""
    mode_pr = {0: "PR", 1: "AB"}.get(state.mode_pr, "unknown")
    print("Robot state:")
    print(f"  Volume: {state.volume}/100")
    print(
        f"  Control: fsm_id={state.fsm_id} ({state.fsm_name}), "
        f"mode_pr={state.mode_pr} ({mode_pr}), "
        f"mode_machine={state.mode_machine}, tick={state.tick}"
    )
    print(
        f"  IMU: rpy={_format_vector(state.imu_rpy)} rad, "
        f"gyro={_format_vector(state.imu_gyroscope)}, "
        f"accel={_format_vector(state.imu_accelerometer)}, "
        f"temperature_raw={state.imu_temperature}"
    )

    if state.battery is None:
        print("  Battery: no rt/lf/bmsstate sample received")
    else:
        battery = state.battery
        cells = battery.cell_voltages_mv
        cell_summary = (
            f"{min(cells)}-{max(cells)} mV across {len(cells)} cells"
            if cells
            else "no cell voltages"
        )
        print(
            f"  Battery: SOC={battery.soc}%, SOH={battery.soh}%, "
            f"cells={cell_summary}, cycles={battery.cycles}"
        )
        print(
            f"    pack_voltage_raw={list(battery.pack_voltages_raw)}, "
            f"current_raw={battery.current_raw}, "
            f"temperature_raw={list(battery.temperatures_raw)}, "
            f"state={list(battery.state)}"
        )

    active_motors = [
        motor
        for motor in state.motors
        if (
            motor.mode
            or motor.state
            or motor.voltage
            or abs(motor.position) > 1e-6
            or abs(motor.velocity) > 1e-6
            or abs(motor.torque) > 1e-6
            or any(motor.temperatures)
        )
    ]
    print(f"  Motors: {len(active_motors)}/{len(state.motors)} reporting activity")
    for motor in active_motors:
        temperatures = ",".join(str(value) for value in motor.temperatures)
        print(
            f"    [{motor.index:02d}] mode={motor.mode} state={motor.state} "
            f"q={motor.position:+.3f} rad dq={motor.velocity:+.3f} "
            f"tau={motor.torque:+.3f} voltage={motor.voltage:.2f} "
            f"temperature_raw=[{temperatures}]"
        )


def main(argv: list[str] | None = None) -> int:
    """Run the requested hardware diagnostic."""
    args = build_parser().parse_args(argv)
    try:
        load_project_configuration(PROJECT_ROOT)
        network_interface = os.getenv(
            "UNITREE_NETWORK_INTERFACE",
            "eth0",
        ).strip()

        if args.command == "microphone":
            interface_ip = os.getenv(
                "UNITREE_INTERFACE_IP",
                "192.168.123.164",
            ).strip()
            microphone_group = os.getenv(
                "UNITREE_MIC_MULTICAST_GROUP",
                "239.168.123.161",
            ).strip()
            microphone_port = int(os.getenv("UNITREE_MIC_PORT", "5555"))
            samples = record_microphone(
                args.duration,
                args.output,
                interface_ip,
                microphone_group,
                microphone_port,
            )
            print(
                f"Microphone OK: {samples} samples saved to "
                f"{args.output.expanduser().resolve()}"
            )
        elif args.command == "photo":
            width, height = take_photo(args.output, network_interface)
            print(
                f"Camera OK: {width}x{height} photo saved to "
                f"{args.output.expanduser().resolve()}"
            )
        elif args.command == "video":
            frames, width, height = record_video(
                args.duration,
                args.output,
                network_interface,
                args.fps,
            )
            print(
                f"Video OK: {frames} frames at {width}x{height} saved to "
                f"{args.output.expanduser().resolve()}"
            )
        elif args.command == "cameras":
            depth_camera = camera_device(
                args.depth_camera
                if args.depth_camera is not None
                else os.getenv("UNITREE_DEPTH_CAMERA_DEVICE", "/dev/video0")
            )
            infrared_camera = camera_device(
                args.infrared_camera
                if args.infrared_camera is not None
                else os.getenv(
                    "UNITREE_INFRARED_CAMERA_DEVICE",
                    "/dev/video2",
                )
            )
            outputs = capture_realsense_outputs(
                args.output_dir,
                network_interface,
                depth_camera,
                infrared_camera,
            )
            print("RealSense cameras OK:")
            for output in outputs:
                print(f"  {output}")
        elif args.command == "speakers":
            samples = test_speakers(
                args.input,
                network_interface,
            )
            print(
                f"Speakers OK: played {samples} samples from "
                f"{args.input.expanduser().resolve()}."
            )
        elif args.command == "state":
            state = read_robot_state(
                network_interface,
                timeout=args.timeout,
            )
            print_robot_state(state)
            state_output = args.output.expanduser().resolve()
            state_output.parent.mkdir(parents=True, exist_ok=True)
            state_output.write_text(
                json.dumps(asdict(state), indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"  Snapshot: {state_output}")
        elif args.command == "volume":
            previous, current = set_robot_volume(
                args.level,
                network_interface,
            )
            if previous == current:
                print(f"Volume unchanged: {current}/100.")
            else:
                print(f"Volume OK: {previous}/100 -> {current}/100.")
        return 0
    except Exception as error:
        print(f"Diagnostic failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
