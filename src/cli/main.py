"""Meshpoint CLI -- argparse dispatcher for management subcommands."""

from __future__ import annotations

import argparse
import subprocess
import sys

from src.version import __version__ as VERSION


def cmd_setup(_args: argparse.Namespace) -> None:
    from src.cli.setup_wizard import run_setup
    run_setup()


def cmd_status(_args: argparse.Namespace) -> None:
    from src.cli.status_command import show_status
    show_status()


def cmd_logs(_args: argparse.Namespace) -> None:
    try:
        subprocess.run(
            ["journalctl", "-u", "meshpoint", "-f", "--no-pager", "-n", "100", "-o", "cat"],
            check=False,
        )
    except KeyboardInterrupt:
        pass


def cmd_restart(_args: argparse.Namespace) -> None:
    print("  Restarting meshpoint service...")
    result = subprocess.run(
        ["sudo", "systemctl", "restart", "meshpoint"],
        check=False,
    )
    if result.returncode == 0:
        print("  Service restarted.")
    else:
        print("  Failed to restart. Check: meshpoint logs")


def cmd_stop(_args: argparse.Namespace) -> None:
    print("  Stopping meshpoint service...")
    subprocess.run(["sudo", "systemctl", "stop", "meshpoint"], check=False)
    print("  Service stopped.")


def cmd_report(_args: argparse.Namespace) -> None:
    from src.cli.report_command import run_report
    run_report()


def run_hwcheck(rest: list[str]) -> None:
    """Delegate to the platform CLI; bare options mean ``check``."""
    from src.hal.platform.cli import main as hwcheck_main
    rest = list(rest)
    if not rest or rest[0] not in ("detect", "reset", "probe", "check", "-h", "--help"):
        rest = ["check", *rest]
    sys.exit(hwcheck_main(rest))


def cmd_hwcheck(args: argparse.Namespace) -> None:
    run_hwcheck(args.hwcheck_args or [])


def cmd_meshcore_radio(args: argparse.Namespace) -> None:
    from src.cli.meshcore_radio_command import run_meshcore_radio
    run_meshcore_radio(args)


def cmd_reset_password(_args: argparse.Namespace) -> None:
    from src.cli.reset_password_command import run_reset_password
    sys.exit(run_reset_password())


def cmd_version(_args: argparse.Namespace) -> None:
    print(f"  Meshpoint v{VERSION}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="meshpoint",
        description="Meshpoint management CLI",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="Run the interactive setup wizard")
    sub.add_parser("status", help="Show device status and health")
    sub.add_parser("report", help="Full operational report (requires running service)")
    sub.add_parser("logs", help="Tail the service logs (journalctl)")
    sub.add_parser("restart", help="Restart the meshpoint service")
    sub.add_parser("stop", help="Stop the meshpoint service")
    hw = sub.add_parser(
        "hwcheck",
        help="Staged hardware proof: kernel, SPI, GPIO, SX1302 chip ID, HAL, RX "
             "(e.g. 'hwcheck --through chip'; 'hwcheck detect')",
    )
    hw.add_argument("hwcheck_args", nargs=argparse.REMAINDER)
    mc = sub.add_parser(
        "meshcore-radio",
        help="Configure MeshCore companion radio frequency",
    )
    mc.add_argument(
        "region",
        nargs="?",
        help="Region preset (US, EU, ANZ) or 'custom'",
    )
    mc.add_argument(
        "--port",
        help="Serial port override (auto-detected if omitted)",
    )

    sub.add_parser(
        "reset-password",
        help="Reset the dashboard admin password (invalidates open sessions)",
    )

    sub.add_parser("version", help="Print version information")

    # argparse.REMAINDER cannot pass a leading "--option" through a
    # sub-parser ("hwcheck --through chip"), so hand those arguments over
    # untouched before argparse sees them.
    if len(sys.argv) > 1 and sys.argv[1] == "hwcheck":
        run_hwcheck(sys.argv[2:])

    args = parser.parse_args()

    dispatch = {
        "setup": cmd_setup,
        "status": cmd_status,
        "report": cmd_report,
        "logs": cmd_logs,
        "restart": cmd_restart,
        "stop": cmd_stop,
        "hwcheck": cmd_hwcheck,
        "meshcore-radio": cmd_meshcore_radio,
        "reset-password": cmd_reset_password,
        "version": cmd_version,
    }

    handler = dispatch.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
