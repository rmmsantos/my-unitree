"""Command-line interface for Unitree G1 control operations."""

from __future__ import annotations

import argparse
import os
import sys

from my_unitree.configuration import (
    find_project_root,
    load_project_configuration,
)
from robot.services import (
    execute_robot_behavior,
    get_robot_mode,
    list_robot_behaviors,
    list_robot_modes,
    resolve_robot_mode,
    set_robot_mode,
)


PROJECT_ROOT = find_project_root()


def build_parser() -> argparse.ArgumentParser:
    """Build the robot control command parser."""
    parser = argparse.ArgumentParser(
        prog="robot",
        description="Inspect and control the Unitree G1.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    command_format = argparse.ArgumentDefaultsHelpFormatter

    mode = commands.add_parser(
        "mode",
        help="get, list, or set the G1 operating mode",
        formatter_class=command_format,
    )
    mode_commands = mode.add_subparsers(dest="mode_command", required=True)
    mode_commands.add_parser(
        "get",
        help="read the current FSM mode",
        formatter_class=command_format,
    )
    mode_commands.add_parser(
        "list",
        help="list known FSM modes",
        formatter_class=command_format,
    )
    mode_set = mode_commands.add_parser(
        "set",
        help="set a supported FSM mode by name or ID",
        formatter_class=command_format,
    )
    mode_set.add_argument(
        "name",
        nargs="+",
        help="mode name or FSM ID, for example: prepared, rest, or 500",
    )
    mode_set.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive safety confirmation",
    )

    behavior = commands.add_parser(
        "behavior",
        help="list or execute official G1 arm behaviors",
        formatter_class=command_format,
    )
    behavior_commands = behavior.add_subparsers(
        dest="behavior_command",
        required=True,
    )
    behavior_commands.add_parser(
        "list",
        help="list arm behaviors supported by the robot",
        formatter_class=command_format,
    )
    behavior_run = behavior_commands.add_parser(
        "run",
        help="execute one whitelisted official arm behavior",
        formatter_class=command_format,
    )
    behavior_run.add_argument(
        "name",
        nargs="+",
        help="behavior name, for example: hug or high five",
    )
    behavior_run.add_argument(
        "--hold",
        type=float,
        default=2.0,
        help="seconds before releasing arms for held poses",
    )
    behavior_run.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive safety confirmation",
    )
    return parser


def _confirm(message: str, *, skip: bool) -> bool:
    if skip:
        return True
    print(message)
    return input("Continue? [y/N] ").strip().lower() in {"y", "yes"}


def _run_mode(args: argparse.Namespace, network_interface: str) -> int:
    if args.mode_command == "get":
        mode = get_robot_mode(network_interface)
        print(
            f"Current robot mode: {mode.name} "
            f"(FSM {mode.fsm_id}) — {mode.description}"
        )
        return 0

    if args.mode_command == "list":
        print("Known G1 FSM modes:")
        for mode in list_robot_modes():
            access = "settable" if mode.settable else "firmware-managed"
            print(
                f"  {mode.fsm_id:>3}  {mode.name:<18} "
                f"[{access}] {mode.description}"
            )
        return 0

    requested_mode = " ".join(args.name)
    target = resolve_robot_mode(requested_mode)
    confirmed = _confirm(
        f"About to enter {target.name!r} (FSM {target.fsm_id}). "
        f"{target.description} Ensure the G1 is supported as necessary "
        "and the area is clear.",
        skip=args.yes,
    )
    if not confirmed:
        print("Mode change cancelled.")
        return 0
    if target.fsm_id == 500:
        print(
            "Running the required damp → stand → prepared sequence. "
            "Wait while the stand-up motion finishes…"
        )
    previous, current = set_robot_mode(requested_mode, network_interface)
    if previous.fsm_id == current.fsm_id:
        print(f"Robot mode unchanged: {current.name} (FSM {current.fsm_id}).")
    else:
        print(
            f"Robot mode changed: {previous.name} (FSM {previous.fsm_id}) -> "
            f"{current.name} (FSM {current.fsm_id})."
        )
    return 0


def _run_behavior(args: argparse.Namespace, network_interface: str) -> int:
    if args.behavior_command == "list":
        behaviors, confirmed_by_robot = list_robot_behaviors(network_interface)
        source = (
            "reported by this robot"
            if confirmed_by_robot
            else "official SDK map; firmware returned no parseable IDs"
        )
        print(f"Available G1 arm behaviors ({source}):")
        for behavior in behaviors:
            suffix = " [returns to release arm]" if behavior.release_after else ""
            print(f"  {behavior.action_id:>2}  {behavior.name}{suffix}")
        return 0

    behavior_name = " ".join(args.name)
    confirmed = _confirm(
        f"About to execute {behavior_name!r}. Ensure the G1 is stable "
        "and nobody is within arm reach.",
        skip=args.yes,
    )
    if not confirmed:
        print("Behavior cancelled.")
        return 0
    behavior = execute_robot_behavior(
        behavior_name,
        network_interface,
        hold=args.hold,
    )
    print(f"Behavior OK: {behavior.name} (ID {behavior.action_id}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run a robot control command."""
    args = build_parser().parse_args(argv)
    try:
        load_project_configuration(PROJECT_ROOT)
        network_interface = os.getenv(
            "UNITREE_NETWORK_INTERFACE",
            "eth0",
        ).strip()
        if args.command == "mode":
            return _run_mode(args, network_interface)
        return _run_behavior(args, network_interface)
    except Exception as error:
        print(f"Robot command failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
