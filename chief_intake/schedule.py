"""Render and manage the user's launchd job through an injectable runner.

The job runs the signed "Chief Intake" app (see launcher.py), not Python directly,
so macOS permission grants attach to a stable, named identity.
"""
from __future__ import annotations

import getpass
import os
import plistlib
import sys
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from typing import Callable

from . import launcher
from .config import Config, config_path

Runner = Callable[[list[str]], CompletedProcess]


class ScheduleError(Exception):
    pass


@dataclass(frozen=True)
class Installed:
    plist: Path
    app: launcher.AppResult


def default_label() -> str:
    return f"com.{getpass.getuser()}.chief-intake"


def intake_command(python_path: str | Path, selected_config: str | Path | None = None) -> list[str]:
    """The command the app runs; the launcher appends the trigger or --preflight."""
    # Preserve the venv interpreter path; resolving its symlink loses the venv.
    return [str(Path(python_path).expanduser().absolute()), "-m", "chief_intake", "run",
            "--config", str(config_path(str(selected_config) if selected_config else None).resolve())]


def render_plist(cfg: Config, launcher_executable: str | Path, label: str | None = None, hour: int = 7,
                 preflight: bool = False) -> str:
    if not 0 <= hour <= 23:
        raise ValueError("hour must be between 0 and 23")
    log = str(cfg.state_dir.expanduser().absolute() / "launchd.log")
    payload = {
        "Label": label or default_label(),
        "ProgramArguments": [str(Path(launcher_executable).expanduser().absolute()),
                             *(["--preflight"] if preflight else [])],
        "EnvironmentVariables": {"PATH": ":".join([str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])},
        "StartCalendarInterval": {"Hour": hour, "Minute": 0},
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    if preflight:
        del payload["StartCalendarInterval"]
    return plistlib.dumps(payload).decode("utf-8")


def _plist_path(label: str, launch_agents_dir: str | Path | None) -> Path:
    if not label or label in {".", ".."} or "/" in label or "\\" in label:
        raise ValueError("invalid launchd label")
    directory = Path(launch_agents_dir) if launch_agents_dir is not None else Path.home() / "Library/LaunchAgents"
    return directory.expanduser().absolute() / f"{label}.plist"


def _checked(runner: Runner, argv: list[str]) -> None:
    result = runner(argv)
    if result.returncode:
        raise ScheduleError(f"{argv[0]} failed: {result.stderr}")


def _ensure_app(runner: Runner, label: str, python_path: str | Path,
                selected_config: str | Path | None, app_dir: str | Path | None) -> launcher.AppResult:
    try:
        return launcher.ensure_app(runner, bundle_id=label, command=intake_command(python_path, selected_config),
                                   app_dir=app_dir)
    except launcher.LauncherError as exc:
        raise ScheduleError(str(exc)) from exc


def install(cfg: Config, runner: Runner, *, python_path: str | Path = sys.executable,
            label: str | None = None, launch_agents_dir: str | Path | None = None,
            uid: int | None = None, hour: int = 7,
            selected_config: str | Path | None = None,
            app_dir: str | Path | None = None) -> Installed:
    label = label or default_label()
    path = _plist_path(label, launch_agents_dir)
    app = _ensure_app(runner, label, python_path, selected_config, app_dir)
    content = render_plist(cfg, app.executable, label, hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.expanduser().mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _checked(runner, ["plutil", "-lint", str(path)])
    domain = f"gui/{os.getuid() if uid is None else uid}"
    runner(["launchctl", "bootout", f"{domain}/{label}"])
    _checked(runner, ["launchctl", "bootstrap", domain, str(path)])
    return Installed(path, app)


def remove(cfg: Config, runner: Runner, *, label: str | None = None,
           launch_agents_dir: str | Path | None = None, uid: int | None = None) -> None:
    # The app stays installed: deleting it would discard the user's permission grants.
    label = label or default_label()
    path = _plist_path(label, launch_agents_dir)
    runner(["launchctl", "bootout", f"gui/{os.getuid() if uid is None else uid}/{label}"])
    path.unlink(missing_ok=True)


def test_kickstart(cfg: Config, runner: Runner, *, label: str | None = None,
                   uid: int | None = None, selected_config: str | Path | None = None,
                   python_path: str | Path = sys.executable,
                   app_dir: str | Path | None = None) -> launcher.AppResult:
    base = label or default_label()
    label = base + ".preflight"
    # Separate one-shot job through the same app; terminal runs cannot consume it.
    _plist_path(label, None)
    app = _ensure_app(runner, base, python_path, selected_config, app_dir)
    state_dir = cfg.state_dir.expanduser().absolute()
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / (label + ".plist")
    path.write_text(render_plist(cfg, app.executable, label, preflight=True), encoding="utf-8")
    domain = f"gui/{os.getuid() if uid is None else uid}"
    runner(["launchctl", "bootout", f"{domain}/{label}"])
    _checked(runner, ["launchctl", "bootstrap", domain, str(path)])
    _checked(runner, ["launchctl", "kickstart", "-k", f"{domain}/{label}"])
    return app


def print_setup_instructions(app: launcher.AppResult) -> str:
    lines = [f"Scheduled runs use the app: {app.app}"]
    if app.rebuilt:
        lines += ["The app was (re)built, so macOS treats it as new. In System Settings > Privacy & Security >",
                  f'Full Disk Access, turn on "{launcher.APP_NAME}" (click + and choose the app above if it is not listed).',
                  "If an older entry for the same app is listed, remove it first."]
    else:
        lines.append(f'The app is unchanged, so any grant you already gave it still applies. If "{launcher.APP_NAME}" '
                     "is not on in System Settings > Privacy & Security > Full Disk Access, turn it on.")
    lines += ["Then run chief-intake schedule test while at the Mac and approve the prompt for "
              f'"{launcher.APP_NAME}" to control Notes.',
              "Check chief-intake doctor for the LaunchAgent preflight result.",
              "Python upgrades do not need a new grant; reinstalling with a different Python or config path does."]
    return "\n".join(lines)
