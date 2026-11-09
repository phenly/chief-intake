"""Build the small signed "Chief Intake" app that launchd runs.

macOS attaches Full Disk Access and Automation grants to the process launchd starts.
Granting them to the venv's Python would name the grant "python3.14", cover every
script that Python runs, and break on each Python upgrade. Instead the job runs this
app, which starts the intake command as a child process that inherits its grants.

The app is ad-hoc signed, so its identity is the hash of its contents: any rebuild
needs a fresh grant. ensure_app therefore rebuilds only when its inputs change.
"""
from __future__ import annotations

import hashlib
import plistlib
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from subprocess import CompletedProcess
from typing import Callable

Runner = Callable[[list[str]], CompletedProcess]

APP_NAME = "Chief Intake"
EXECUTABLE = "chief-intake-launcher"


class LauncherError(Exception):
    pass


@dataclass(frozen=True)
class AppResult:
    app: Path
    rebuilt: bool

    @property
    def executable(self) -> Path:
        return executable_path(self.app)


def default_app_dir() -> Path:
    return Path.home() / "Applications"


def app_path(app_dir: str | Path | None = None) -> Path:
    directory = Path(app_dir) if app_dir is not None else default_app_dir()
    return directory.expanduser().absolute() / f"{APP_NAME}.app"


def executable_path(app: Path) -> Path:
    return app / "Contents/MacOS" / EXECUTABLE


def _source() -> str:
    return resources.files("chief_intake").joinpath("launcher.swift").read_text(encoding="utf-8")


def render_info(bundle_id: str, command: list[str]) -> dict:
    return {
        "CFBundleIdentifier": bundle_id,
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": EXECUTABLE,
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1",
        "CFBundleShortVersionString": "1.0",
        "LSUIElement": True,  # no Dock icon or menu bar
        "ChiefIntakeCommand": command,
        "ChiefIntakeLauncherSHA256": hashlib.sha256(_source().encode("utf-8")).hexdigest(),
    }


def _current(app: Path, info: dict, runner: Runner) -> bool:
    try:
        existing = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    if existing != info or not executable_path(app).is_file():
        return False
    return runner(["codesign", "--verify", "--strict", str(app)]).returncode == 0


def _checked(runner: Runner, argv: list[str]) -> None:
    result = runner(argv)
    if result.returncode:
        raise LauncherError(f"{argv[0]} failed: {result.stderr}")


def ensure_app(runner: Runner, *, bundle_id: str, command: list[str],
               app_dir: str | Path | None = None) -> AppResult:
    """Build, sign and install the app unless an identical, validly signed one exists."""
    app = app_path(app_dir)
    info = render_info(bundle_id, command)
    if _current(app, info, runner):
        return AppResult(app, rebuilt=False)
    app.parent.mkdir(parents=True, exist_ok=True)
    # Build beside the destination so the final swap is a same-volume rename.
    with tempfile.TemporaryDirectory(prefix=".chief-intake-build-", dir=app.parent) as tmp:
        staged = Path(tmp) / app.name
        (staged / "Contents/MacOS").mkdir(parents=True)
        (staged / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
        source = Path(tmp) / "launcher.swift"
        source.write_text(_source(), encoding="utf-8")
        _checked(runner, ["swiftc", "-O", "-o", str(executable_path(staged)), str(source)])
        if not executable_path(staged).is_file():
            raise LauncherError("swiftc reported success but produced no launcher binary")
        _checked(runner, ["codesign", "--force", "--sign", "-", "--identifier", bundle_id, str(staged)])
        _checked(runner, ["codesign", "--verify", "--strict", str(staged)])
        old = Path(tmp) / "previous.app"
        if app.exists():
            app.rename(old)
        staged.rename(app)
    return AppResult(app, rebuilt=True)
