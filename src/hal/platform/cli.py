"""``python -m src.hal.platform`` and ``meshpoint hwcheck``.

Sub-commands
  detect          print the detected platform (--json / --shell)
  reset [--hold]  run the platform GPIO sequence (systemd ExecStartPre /
                  ExecStopPost); no-op for non-native platforms
  probe           read the SX1302 chip-version register (read-only)
  check           staged hardware proof, stop at the first broken layer

``check`` stages, in order (each needs the previous to pass):
  linux  kernel, device tree, platform, kernel pin      (passive)
  spi    spidev node exists and is openable             (passive)
  gpio   sysfs GPIO present and covers the lines        (passive)
  chip   run reset sequence, read chip-version reg      (ACTIVE: SPI+GPIO)
  hal    libloragw configure + lgw_start + lgw_stop     (ACTIVE)
  rx     listen N seconds, print Meshtastic headers     (ACTIVE)

Active stages refuse to run while the meshpoint service is active:
toggling reset or sharing SPI would corrupt a live concentrator.
"""

from __future__ import annotations

import argparse
import json
import os
import platform as stdlib_platform
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from src.hal.platform import chip_probe, detect, sequencer
from src.hal.platform.gpio import GpioError, SysfsGpio
from src.hal.platform.profiles import PlatformProfile

PASS, FAIL, WARN, INFO, UNKNOWN = "PASS", "FAIL", "WARN", "INFO", "UNKNOWN"
KERNEL_LOCK = "/etc/meshpoint/bobcat-kernel.lock"
HELD_PREFIXES = ("linux-image-", "linux-dtb-", "linux-u-boot-")
STAGES = ("linux", "spi", "gpio", "chip", "hal", "rx")


@dataclass
class Check:
    stage: str
    name: str
    status: str
    detail: str = ""
    hint: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, *args, **kw) -> Check:
        c = Check(*args, **kw)
        self.checks.append(c)
        return c

    def failed(self, stage: str | None = None) -> bool:
        return any(
            c.status == FAIL and (stage is None or c.stage == stage)
            for c in self.checks
        )


# ── helpers ──────────────────────────────────────────────────────────

def service_active() -> bool:
    if not shutil.which("systemctl"):
        return False
    r = subprocess.run(
        ["systemctl", "is-active", "--quiet", "meshpoint"], check=False
    )
    return r.returncode == 0


def _read(path: str) -> str:
    try:
        return Path(path).read_text(errors="replace").strip("\x00\n ")
    except OSError:
        return ""


def resolve_spi(det: detect.Detection, requested: Optional[str]) -> str:
    """Explicit --spi, else the platform's device, else the Pi default."""
    if requested and requested != "auto":
        return requested
    return det.profile.spi_device or "/dev/spidev0.0"


# ── stages ───────────────────────────────────────────────────────────

def stage_linux(rep: Report, det: detect.Detection) -> None:
    s = "linux"
    rep.add(s, "kernel", INFO, f"{stdlib_platform.release()} ({stdlib_platform.machine()})")
    py = sys.version_info
    rep.add(s, "python", PASS if py >= (3, 12) else WARN,
            f"{py.major}.{py.minor}.{py.micro}",
            "" if py >= (3, 12) else "Meshpoint documents Python 3.12+")
    model = _read("/proc/device-tree/model")
    rep.add(s, "device-tree model", INFO if model else WARN, model or "not readable")
    rel = _read("/etc/armbian-release")
    if rel:
        wanted = [ln for ln in rel.splitlines() if ln.split("=")[0] in
                  ("BOARD", "BOARDFAMILY", "VERSION", "BRANCH", "LINUXFAMILY")]
        rep.add(s, "armbian-release", INFO, "; ".join(wanted))

    # The SX1302 HAL probes /dev/i2c-1 for a temperature sensor on start.
    # What else lives on that bus is UNKNOWN for the G285: list adapters
    # (passive, sysfs only) so the operator can report them.
    adapters = []
    for entry in sorted(Path("/sys/class/i2c-dev").glob("i2c-*")):
        adapters.append(f"{entry.name}={_read(str(entry / 'name')) or '?'}")
    if adapters:
        rep.add(s, "i2c adapters", INFO, "; ".join(adapters))

    p = det.profile
    detail = f"{p.id} ({det.source}, confidence {det.confidence})"
    if p.supported:
        rep.add(s, "platform", PASS, detail)
    else:
        rep.add(
            s, "platform", FAIL, detail,
            "; ".join(det.warnings) or "; ".join(p.notes),
        )
    for ev in det.evidence:
        rep.add(s, "  evidence", INFO, ev)
    for w in det.warnings:
        rep.add(s, "  warning", WARN, w)

    if det.is_bobcat and p.supported:
        _check_kernel_pin(rep)


def _check_kernel_pin(rep: Report) -> None:
    s = "linux"
    lock = detect.parse_env_file(_read(KERNEL_LOCK))
    cur = stdlib_platform.release()
    if not lock:
        rep.add(
            s, "kernel pin", WARN, f"{KERNEL_LOCK} missing",
            "run: sudo bash /opt/meshpoint/scripts/install.sh (records the "
            "working kernel and holds the boot packages)",
        )
    elif lock.get("kernel_release") != cur:
        rep.add(
            s, "kernel pin", FAIL,
            f"running {cur}, installed under {lock.get('kernel_release')}",
            "kernel drifted since Meshpoint was installed: restore the "
            "Bobcat-Armbian image/kernel (docs/BOBCAT-G285.md Recovery)",
        )
    else:
        rep.add(s, "kernel pin", PASS, f"running kernel matches lock ({cur})")

    if shutil.which("apt-mark"):
        held = subprocess.run(
            ["apt-mark", "showhold"], capture_output=True, text=True, check=False
        ).stdout.split()
        need = [h for h in held if h.startswith(HELD_PREFIXES)]
        if need:
            rep.add(s, "apt holds", PASS, ", ".join(need))
        else:
            rep.add(
                s, "apt holds", FAIL, "no linux-image/dtb/u-boot package is held",
                "sudo apt-mark hold $(dpkg-query -W -f='${Package}\\n' "
                "'linux-image-*' 'linux-dtb-*' 'linux-u-boot-*')",
            )


def stage_spi(rep: Report, det: detect.Detection, spi: str) -> None:
    s = "spi"
    nodes = sorted(str(p) for p in Path("/dev").glob("spidev*"))
    rep.add(s, "spidev nodes", INFO if nodes else WARN, ", ".join(nodes) or "none")
    want = det.profile.spi_device
    if want and spi != want:
        rep.add(s, "configured SPI", WARN,
                f"using {spi}; {det.profile.id} expects {want}")
    if not os.path.exists(spi):
        hint = {
            "bobcat_g285": "G285 should expose /dev/spidev1.0 from the shipped "
                           "device tree. UNKNOWN whether an overlay is needed: "
                           "check /boot/armbianEnv.txt and `ls /sys/class/spi_master`.",
            "bobcat_g29x": "add `overlays=spi5-m1` to /boot/armbianEnv.txt and reboot.",
        }.get(det.profile.id, "enable SPI (raspi-config) and reboot.")
        rep.add(s, "device node", FAIL, f"{spi} missing", hint)
        return
    st = os.stat(spi)
    rep.add(s, "device node", PASS, f"{spi} mode {oct(st.st_mode & 0o777)} uid {st.st_uid} gid {st.st_gid}")
    if os.access(spi, os.R_OK | os.W_OK):
        rep.add(s, "open rw", PASS, "readable and writable by this user")
    else:
        rep.add(s, "open rw", FAIL, f"permission denied for uid {os.getuid()}",
                "run with sudo, or install the udev rule "
                "(/etc/udev/rules.d/60-meshpoint-spidev.rules; re-run install.sh)")


def stage_gpio(rep: Report, det: detect.Detection) -> None:
    s = "gpio"
    p = det.profile
    if not p.native_gpio:
        rep.add(s, "gpio", INFO, f"{p.id} uses the legacy pinctrl path; skipped")
        return
    g = SysfsGpio()
    if not g.available():
        rep.add(
            s, "sysfs gpio", FAIL, "/sys/class/gpio/export missing",
            "kernel lacks CONFIG_GPIO_SYSFS. UNKNOWN for the G285 image. "
            "Needs a libgpiod backend (not implemented) - report this output.",
        )
        return
    rep.add(s, "sysfs gpio", PASS, "/sys/class/gpio/export present")
    for chip in g.chips():
        rep.add(s, f"  {chip.name}", INFO,
                f"base {chip.base} ngpio {chip.ngpio} label {chip.label!r}")
    opts = sequencer.SequenceOptions.from_env(detect.effective_env())
    lines, _ = sequencer.plan(p, "start", opts)
    for ln in lines:
        chip = g.chip_for(ln.number)
        if chip is None:
            rep.add(s, f"line {ln.name}", FAIL, f"GPIO{ln.number} not covered by any gpiochip",
                    "sysfs numbering differs from bank*32+offset on this kernel")
            continue
        state = ""
        if g.is_exported(ln.number):
            try:
                state = f", currently {g.read(ln.number)}"
            except GpioError:
                pass
        rep.add(
            s, f"line {ln.name}", PASS,
            f"GPIO{ln.number} = {ln.rockchip_name} on {chip.name} "
            f"(evidence: {ln.evidence.value}){state}",
        )
    if "pa_enable" not in {ln.name for ln in lines} and "pa_enable" in p.optional_lines:
        rep.add(s, "pa_enable", UNKNOWN,
                "GPIO147 not driven on this model (no G285 evidence); set "
                "MESHPOINT_PA_GPIO=147 in /etc/meshpoint/platform.env if TX is silent")


def stage_chip(rep: Report, det: detect.Detection, spi: str, reset: bool) -> None:
    s = "chip"
    p = det.profile
    if reset:
        if p.native_gpio:
            try:
                opts = sequencer.SequenceOptions.from_env(detect.effective_env())
                log = sequencer.run(p, "start", options=opts)
                rep.add(s, "reset sequence", PASS, f"{len(log)} actions")
                for a in log:
                    rep.add(s, "  " + a, INFO)
            except (GpioError, PermissionError) as exc:
                rep.add(s, "reset sequence", FAIL, str(exc),
                        "run as root; check /sys/class/gpio is writable")
                return
        else:
            script = Path(__file__).resolve().parents[3] / "scripts" / "reset_concentrator.sh"
            r = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=False)
            rep.add(s, "reset script", PASS if r.returncode == 0 else FAIL,
                    (r.stdout + r.stderr).strip())
            if r.returncode != 0:
                return
    res = chip_probe.probe_chip(spi)
    rep.add(
        s, "SX1302 version register", PASS if res.ok else (WARN if res.alive else FAIL),
        res.message(),
        "" if res.alive else _chip_hint(p, res),
    )


def _chip_hint(p: PlatformProfile, res: chip_probe.ProbeResult) -> str:
    st = chip_probe.ProbeState
    return {
        st.DEAD: "check power/reset sequence (hwcheck gpio), antenna not required; "
                 "fully power-cycle (unplug 10 s); confirm kernel pin",
        st.FLOATING: "SPI pins not muxed or wrong bus/CS: verify the spidev node "
                     "and overlay for this model",
        st.UNSTABLE: "intermittent SPI: power/ground problem or wrong clock; power-cycle",
        st.MISSING: "device node absent: see stage 'spi'",
        st.NO_PERMISSION: "run with sudo or fix spidev permissions",
        st.IO_ERROR: "ioctl failed: SPI controller driver not bound?",
    }.get(res.state, "")


def _load_radio():
    """Region/frequency from config/local.yaml - never a silent default."""
    try:
        from src.config import load_config  # noqa: PLC0415
    except Exception as exc:  # yaml missing => no venv
        return None, f"cannot import Meshpoint config ({exc})"
    path = os.environ.get("CONCENTRATOR_CONFIG", "config/local.yaml")
    if not Path(path).exists():
        return None, f"{path} not found - run `meshpoint setup` or pass --region"
    cfg = load_config()
    return cfg, ""


def _start_hal(rep: Report, spi: str, region: Optional[str], stage: str):
    from src.hal.concentrator_config import ConcentratorChannelPlan  # noqa: PLC0415
    from src.hal.sx1302_wrapper import SX1302Wrapper  # noqa: PLC0415

    cfg, why = _load_radio()
    syncword = 0x2B
    if region:
        plan = ConcentratorChannelPlan.for_region(region)
        rep.add(stage, "radio plan", INFO, f"region {region} LongFast default (from --region)")
    elif cfg is not None:
        r = cfg.radio
        plan = ConcentratorChannelPlan.from_radio_config(
            r.region, r.frequency_mhz, r.spreading_factor, r.bandwidth_khz
        )
        syncword = r.sync_word
        rep.add(stage, "radio plan", INFO,
                f"region {r.region} {r.frequency_mhz} MHz SF{r.spreading_factor} "
                f"BW{r.bandwidth_khz:g} sync 0x{r.sync_word:02X} (config/local.yaml)")
    else:
        rep.add(stage, "radio plan", FAIL, why,
                "refusing to pick a region silently: pass --region US|EU_868|ANZ|IN|KR|SG_923")
        return None
    w = SX1302Wrapper(spi_path=spi)
    try:
        w.load()
        w.configure(plan)
        w.start()
        w.set_syncword(syncword)
    except Exception as exc:  # noqa: BLE001
        rep.add(stage, "lgw_start", FAIL, f"{type(exc).__name__}: {exc}",
                "see HAL output above; the chip stage must PASS first")
        try:
            w.stop()
        except Exception:  # noqa: BLE001
            pass
        return None
    rep.add(stage, "lgw_start", PASS, "concentrator started")
    return w


def stage_hal(rep: Report, spi: str, region: Optional[str]) -> None:
    w = _start_hal(rep, spi, region, "hal")
    if w is not None:
        w.stop()
        rep.add("hal", "lgw_stop", PASS, "concentrator stopped")


def parse_mesh_header(payload: bytes) -> Optional[dict]:
    """Meshtastic 2.x over-the-air header (16 bytes, little endian)."""
    if len(payload) < 16:
        return None
    to, frm, pid = struct.unpack_from("<III", payload, 0)
    flags, chash, next_hop, relay = payload[12:16]
    return {
        "dest": f"!{to:08x}",
        "sender": f"!{frm:08x}",
        "packet_id": f"0x{pid:08x}",
        "hop_limit": flags & 0x07,
        "want_ack": bool(flags & 0x08),
        "via_mqtt": bool(flags & 0x10),
        "hop_start": (flags >> 5) & 0x07,
        "channel_hash": f"0x{chash:02x}",
        "next_hop": f"0x{next_hop:02x}",
        "relay_node": f"0x{relay:02x}",
    }


def stage_rx(rep: Report, spi: str, region: Optional[str], seconds: float) -> None:
    from src.hal.sx1302_wrapper import BW_MAP  # noqa: PLC0415

    w = _start_hal(rep, spi, region, "rx")
    if w is None:
        return
    seen = 0
    t_end = time.monotonic() + seconds
    try:
        while time.monotonic() < t_end:
            for p in w.receive():
                seen += 1
                hdr = parse_mesh_header(p.payload) or {}
                rep.add(
                    "rx", f"packet {seen}", INFO,
                    f"{time.strftime('%H:%M:%S')} {p.frequency_hz/1e6:.3f}MHz "
                    f"SF{p.spreading_factor} BW{BW_MAP.get(p.bandwidth, p.bandwidth)} "
                    f"rssi {p.rssi:.1f} snr {p.snr:.1f} len {len(p.payload)} "
                    f"crc_ok={p.crc_ok} {hdr}",
                )
            time.sleep(0.01)
    finally:
        w.stop()
    rep.add("rx", "summary", PASS if seen else WARN,
            f"{seen} CRC-OK packet(s) in {seconds:g}s; "
            f"crc_bad={w.crc_bad_count} no_crc={w.no_crc_count}",
            "" if seen else "no traffic heard: check antenna, region/frequency, "
            "and that a Meshtastic node is transmitting on the same preset/slot")


# ── command plumbing ─────────────────────────────────────────────────

def _print_report(rep: Report) -> None:
    last = None
    for c in rep.checks:
        if c.stage != last:
            print(f"\n== {c.stage} ==")
            last = c.stage
        line = f"[{c.status:7}] {c.name}"
        if c.detail:
            line += f": {c.detail}"
        print(line)
        if c.hint and c.status in (FAIL, WARN, UNKNOWN):
            print(f"           -> {c.hint}")


def cmd_detect(args) -> int:
    det = detect.detect_platform()
    if args.shell:
        print(f"MP_PLATFORM={det.profile.id}")
        print(f"MP_PLATFORM_SUPPORTED={'1' if det.profile.supported else '0'}")
        print(f"MP_SPI_DEVICE={det.profile.spi_device or ''}")
        print(f"MP_PLATFORM_CONFIDENCE={det.confidence}")
        return 0
    if args.json:
        d = asdict(det)
        d["profile"] = {"id": det.profile.id, "label": det.profile.label,
                        "spi_device": det.profile.spi_device,
                        "supported": det.profile.supported}
        print(json.dumps(d, indent=2))
        return 0
    print(f"platform:   {det.profile.id}  ({det.profile.label})")
    print(f"decided by: {det.source}, confidence {det.confidence}")
    print(f"supported:  {det.profile.supported}")
    print(f"spi device: {det.profile.spi_device or '(from config)'}")
    for e in det.evidence:
        print(f"evidence:   {e}")
    for w in det.warnings:
        print(f"WARNING:    {w}")
    return 0 if det.profile.supported else 2


def _ensure_spi_access(profile: PlatformProfile) -> None:
    """Give the unprivileged service user rw access to the SPI node.

    Runs as root from ExecStartPre on every start, so it survives the
    node being recreated at boot without depending on udev ordering.
    """
    spi = profile.spi_device
    if not spi or not os.path.exists(spi):
        return
    if getattr(os, "geteuid", lambda: 1)() != 0:
        return
    try:
        import grp  # noqa: PLC0415

        gid = grp.getgrnam("meshpoint").gr_gid
        os.chown(spi, -1, gid)
        os.chmod(spi, 0o660)
        print(f"  {spi}: group meshpoint, mode 0660")
    except (KeyError, OSError, ImportError) as exc:
        print(f"  WARNING: could not grant meshpoint access to {spi}: {exc}",
              file=sys.stderr)


def cmd_reset(args) -> int:
    det = detect.detect_platform()
    p = det.profile
    if not p.supported:
        print(f"meshpoint: platform {p.id} is not supported: "
              + "; ".join(det.warnings or p.notes), file=sys.stderr)
        return 2
    if not p.native_gpio:
        print(f"{p.id}: no native GPIO sequence (legacy reset script handles it)")
        return 0
    mode = "hold" if args.hold else "start"
    try:
        opts = sequencer.SequenceOptions.from_env(detect.effective_env())
        log = sequencer.run(p, mode, options=opts, log=lambda m: print(f"  {m}", flush=True))
    except (GpioError, ValueError, PermissionError) as exc:
        print(f"meshpoint: {p.id} GPIO {mode} failed: {exc}", file=sys.stderr)
        return 1
    if not args.hold:
        _ensure_spi_access(p)
    print(f"{p.id}: concentrator {'held in reset' if args.hold else 'reset'} "
          f"({len(log)} actions)")
    return 0


def cmd_probe(args) -> int:
    det = detect.detect_platform()
    spi = resolve_spi(det, args.spi)
    res = chip_probe.probe_chip(spi)
    print(res.message())
    return 0 if res.alive else 1


def cmd_check(args) -> int:
    det = detect.detect_platform()
    spi = resolve_spi(det, args.spi)
    rep = Report()
    through = STAGES.index(args.through)

    stage_linux(rep, det)
    if through >= 1 and not rep.failed("linux"):
        stage_spi(rep, det, spi)
    if through >= 2 and not rep.failed():
        stage_gpio(rep, det)
    active = through >= 3
    if active and not rep.failed():
        if service_active() and not args.force:
            rep.add("chip", "service", FAIL, "meshpoint service is running",
                    "sudo systemctl stop meshpoint, run hwcheck, then start it again")
        else:
            stage_chip(rep, det, spi, reset=not args.no_reset)
    if through == 4 and not rep.failed():
        stage_hal(rep, spi, args.region)
    if through == 5 and not rep.failed():
        stage_rx(rep, spi, args.region, args.seconds)

    if args.json:
        print(json.dumps([asdict(c) for c in rep.checks], indent=2))
    else:
        _print_report(rep)
        print()
        print("RESULT: " + ("FAIL - fix the first FAIL above before going further"
                            if rep.failed() else "PASS through stage '%s'" % args.through))
    return 1 if rep.failed() else 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="meshpoint hwcheck")
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("detect")
    d.add_argument("--json", action="store_true")
    d.add_argument("--shell", action="store_true")
    r = sub.add_parser("reset")
    r.add_argument("--hold", action="store_true")
    pr = sub.add_parser("probe")
    pr.add_argument("--spi")
    c = sub.add_parser("check")
    c.add_argument("--through", choices=STAGES, default="gpio",
                   help="last stage to run (default: gpio = passive only)")
    c.add_argument("--spi", help="override spidev path")
    c.add_argument("--region", help="US|EU_868|ANZ|IN|KR|SG_923 (else config/local.yaml)")
    c.add_argument("--seconds", type=float, default=60.0, help="rx listen time")
    c.add_argument("--no-reset", action="store_true", help="skip GPIO reset in chip stage")
    c.add_argument("--force", action="store_true", help="run active stages even if service is up (unsafe)")
    c.add_argument("--json", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd is None:
        args = ap.parse_args(["check"])
    return {
        "detect": cmd_detect, "reset": cmd_reset,
        "probe": cmd_probe, "check": cmd_check,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
