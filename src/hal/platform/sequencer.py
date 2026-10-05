"""Turn a platform profile into an executed GPIO power/reset sequence."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, replace
from typing import Callable, Mapping, Optional

from src.hal.platform.gpio import GpioError, SysfsGpio
from src.hal.platform.profiles import (
    GpioLine,
    Op,
    PlatformProfile,
    Step,
)

logger = logging.getLogger(__name__)

ENV_PA_GPIO = "MESHPOINT_PA_GPIO"
ENV_RESET_HOLD = "CONCENTRATOR_RESET_HOLD_SEC"

_OFF_WORDS = {"off", "none", "no", "false", "0", "disabled"}


@dataclass(frozen=True)
class SequenceOptions:
    """Operator overrides, normally read from the environment.

    ``pa_gpio``: ``None`` keep the profile default; ``0`` disable the PA
    line; any other number drive that sysfs GPIO as the PA-enable line.
    """

    pa_gpio: Optional[int] = None
    reset_hold_sec: Optional[float] = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SequenceOptions":
        env = os.environ if env is None else env
        raw = (env.get(ENV_PA_GPIO) or "").strip().lower()
        pa: Optional[int] = None
        if raw:
            if raw in _OFF_WORDS:
                pa = 0
            else:
                try:
                    pa = int(raw)
                except ValueError as exc:
                    raise ValueError(
                        f"{ENV_PA_GPIO}={raw!r}: expected a GPIO number or 'off'"
                    ) from exc
        hold_raw = (env.get(ENV_RESET_HOLD) or "").strip()
        hold = float(hold_raw) if hold_raw else None
        return cls(pa_gpio=pa, reset_hold_sec=hold)


def plan(
    profile: PlatformProfile,
    mode: str,
    options: SequenceOptions | None = None,
) -> tuple[tuple[GpioLine, ...], list[Step]]:
    """Return (lines, steps) for ``mode`` in {'start', 'hold'}.

    The PA-enable line is first stripped, then re-inserted immediately
    before the first reset step when enabled. That keeps one rule for
    both models: G29x enables it by default (field-validated), G285 only
    when the operator opts in.
    """
    if not profile.native_gpio:
        raise ValueError(f"{profile.id} has no native GPIO sequence")
    if mode not in ("start", "hold"):
        raise ValueError(f"unknown sequence mode {mode!r}")
    options = options or SequenceOptions()

    lines = list(profile.lines)
    base = list(profile.start_steps if mode == "start" else profile.hold_steps)

    had_pa_by_default = any(s.line == "pa_enable" for s in base)
    base = [s for s in base if s.line != "pa_enable"]

    if options.pa_gpio == 0:
        pa_enabled = False
    elif options.pa_gpio is not None:
        pa_enabled = True
        idx = next(i for i, ln in enumerate(lines) if ln.name == "pa_enable")
        lines[idx] = replace(lines[idx], number=options.pa_gpio)
    else:
        pa_enabled = had_pa_by_default

    if mode == "start" and pa_enabled:
        insert_at = next(
            (i for i, s in enumerate(base) if s.line == "reset"), len(base)
        )
        base[insert_at:insert_at] = [
            Step(Op.OUT, line="pa_enable"),
            Step(Op.SET, line="pa_enable", value=1),
        ]

    if options.reset_hold_sec is not None and mode == "start":
        base = _apply_hold(base, options.reset_hold_sec)

    if not pa_enabled:
        lines = [ln for ln in lines if ln.name != "pa_enable"]
    return tuple(lines), base


def _apply_hold(steps: list[Step], hold: float) -> list[Step]:
    """Replace the sleep that follows reset=1 with ``hold`` seconds."""
    out: list[Step] = []
    after_assert = False
    for s in steps:
        if s.op is Op.SET and s.line == "reset" and s.value == 1:
            after_assert = True
        elif s.op is Op.SLEEP and after_assert:
            out.append(Step(Op.SLEEP, seconds=hold))
            after_assert = False
            continue
        out.append(s)
    return out


def run(
    profile: PlatformProfile,
    mode: str,
    *,
    options: SequenceOptions | None = None,
    gpio: SysfsGpio | None = None,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Execute the sequence; returns a human-readable action log."""
    lines, steps = plan(profile, mode, options)
    by_name = {ln.name: ln for ln in lines}
    gpio = gpio or SysfsGpio()
    actions: list[str] = []

    def emit(msg: str) -> None:
        actions.append(msg)
        (log or logger.debug)(msg)

    for step in steps:
        if step.op is Op.SLEEP:
            emit(f"sleep {step.seconds:g}s")
            sleep(step.seconds)
            continue
        ln = by_name[step.line]  # type: ignore[index]
        try:
            if step.op is Op.OUT:
                gpio.export(ln.number)
                gpio.direction(ln.number, "out")
                emit(f"{ln.name} (GPIO{ln.number} {ln.rockchip_name}): out")
            elif step.op is Op.SET:
                gpio.write(ln.number, int(step.value or 0))
                emit(f"{ln.name} (GPIO{ln.number}): = {int(step.value or 0)}")
            elif step.op is Op.RELEASE:
                gpio.direction(ln.number, "in")
                gpio.unexport(ln.number)
                emit(f"{ln.name} (GPIO{ln.number}): released")
        except GpioError as exc:
            raise GpioError(
                f"{profile.id} {mode} sequence failed at "
                f"{ln.name} (GPIO{ln.number}): {exc}"
            ) from exc
    return actions
