"""Render and manage the user's launchd job through an injectable runner."""
from __future__ import annotations

import getpass
import os
import plistlib
import sys
from pathlib import Path
from subprocess import CompletedProcess
from typing import Callable

from .config import Config, config_path

Runner = Callable[[list[str]], CompletedProcess]


class ScheduleError(Exception):
    pass


def default_label() -> str:
    return f"com.{getpass.getuser()}.chief-intake"


def render_plist(cfg: Config, python_path: str | Path, label: str | None = None, hour: int = 7,
                 selected_config: str | Path | None = None, preflight: bool = False) -> str:
    if not 0 <= hour <= 23:
        raise ValueError("hour must be between 0 and 23")
    log = str(cfg.state_dir.expanduser().absolute() / "launchd.log")
    payload = {
        "Label": label or default_label(),
        # Preserve the venv interpreter path; resolving its symlink loses the venv.
        "ProgramArguments": [str(Path(python_path).expanduser().absolute()), "-m", "chief_intake", "run",
                             "--config", str(config_path(str(selected_config) if selected_config else None).resolve()),
                             *(["--preflight"] if preflight else ["--trigger", "launchd"])],
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


def install(cfg: Config, runner: Runner, *, python_path: str | Path = sys.executable,
            label: str | None = None, launch_agents_dir: str | Path | None = None,
            uid: int | None = None, hour: int = 7,
            selected_config: str | Path | None = None) -> Path:
    label = label or default_label()
    path = _plist_path(label, launch_agents_dir)
    content = render_plist(cfg, python_path, label, hour, selected_config)
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.expanduser().mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _checked(runner, ["plutil", "-lint", str(path)])
    domain = f"gui/{os.getuid() if uid is None else uid}"
    runner(["launchctl", "bootout", f"{domain}/{label}"])
    _checked(runner, ["launchctl", "bootstrap", domain, str(path)])
    return path


def remove(cfg: Config, runner: Runner, *, label: str | None = None,
           launch_agents_dir: str | Path | None = None, uid: int | None = None) -> None:
    label = label or default_label()
    path = _plist_path(label, launch_agents_dir)
    runner(["launchctl", "bootout", f"gui/{os.getuid() if uid is None else uid}/{label}"])
    path.unlink(missing_ok=True)


def test_kickstart(cfg: Config, runner: Runner, *, label: str | None = None,
                   uid: int | None = None, selected_config: str | Path | None = None,
                   python_path: str | Path = sys.executable) -> None:
    label = (label or default_label()) + ".preflight"
    # Separate one-shot job with explicit arguments; terminal runs cannot consume it.
    _plist_path(label, None)
    state_dir = cfg.state_dir.expanduser().absolute()
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / (label + ".plist")
    path.write_text(render_plist(cfg, python_path, label, selected_config=selected_config,
                                 preflight=True), encoding="utf-8")
    domain = f"gui/{os.getuid() if uid is None else uid}"
    runner(["launchctl", "bootout", f"{domain}/{label}"])
    _checked(runner, ["launchctl", "bootstrap", domain, str(path)])
    _checked(runner, ["launchctl", "kickstart", "-k", f"{domain}/{label}"])


def print_setup_instructions(python_path: str | Path) -> str:
    return ("Grant Full Disk Access in System Settings > Privacy & Security to:\n"
            f"{os.path.realpath(Path(python_path).expanduser())}\n"
            "Then run chief-intake schedule test while at the Mac and approve the control Notes prompt.\n"
            "Check chief-intake doctor for the LaunchAgent preflight result.\n"
            "After a Python upgrade, grant access to the new resolved binary and repeat schedule test.")
