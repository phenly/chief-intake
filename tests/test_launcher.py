import plistlib
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from chief_intake import launcher


def runner(calls, verify_ok=True):
    def run(argv):
        calls.append(argv)
        if argv[0] == "swiftc":
            Path(argv[argv.index("-o") + 1]).write_text("synthetic binary")
        ok = verify_ok or "--verify" not in argv or ".chief-intake-build-" in argv[-1]
        return CompletedProcess(argv, 0 if ok else 1, "", "")
    return run


COMMAND = ["/synthetic/venv/bin/python", "-m", "chief_intake", "run", "--config", "/synthetic/config.toml"]


def test_build_names_and_signs_app(tmp_path):
    calls = []
    result = launcher.ensure_app(runner(calls), bundle_id="com.sam.chief-intake", command=COMMAND, app_dir=tmp_path)
    assert result.app == tmp_path / "Chief Intake.app" and result.rebuilt
    assert result.executable.read_text() == "synthetic binary"
    info = plistlib.loads((result.app / "Contents/Info.plist").read_bytes())
    assert info["CFBundleName"] == "Chief Intake" and info["LSUIElement"] is True
    assert info["CFBundleExecutable"] == "chief-intake-launcher"
    assert info["ChiefIntakeCommand"] == COMMAND
    sign = next(c for c in calls if "--sign" in c)
    assert sign[sign.index("--sign") + 1] == "-" and "com.sam.chief-intake" in sign
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".chief-intake-build-")]


def test_unchanged_app_is_not_rebuilt(tmp_path):
    calls = []
    launcher.ensure_app(runner(calls), bundle_id="com.sam.chief-intake", command=COMMAND, app_dir=tmp_path)
    calls.clear()
    again = launcher.ensure_app(runner(calls), bundle_id="com.sam.chief-intake", command=COMMAND, app_dir=tmp_path)
    assert not again.rebuilt
    assert calls == [["codesign", "--verify", "--strict", str(again.app)]]


@pytest.mark.parametrize("change", ["command", "signature"])
def test_changed_inputs_or_broken_signature_rebuild(tmp_path, change):
    calls = []
    launcher.ensure_app(runner(calls), bundle_id="com.sam.chief-intake", command=COMMAND, app_dir=tmp_path)
    command = COMMAND[:-1] + ["/synthetic/other.toml"] if change == "command" else COMMAND
    again = launcher.ensure_app(runner(calls, verify_ok=change != "signature"), bundle_id="com.sam.chief-intake",
                                command=command, app_dir=tmp_path)
    assert again.rebuilt
    assert plistlib.loads((again.app / "Contents/Info.plist").read_bytes())["ChiefIntakeCommand"] == command


def test_compile_failure_keeps_existing_app(tmp_path):
    calls = []
    first = launcher.ensure_app(runner(calls), bundle_id="com.sam.chief-intake", command=COMMAND, app_dir=tmp_path)

    def failing(argv):
        return CompletedProcess(argv, 1, "", "synthetic compile error") if argv[0] == "swiftc" else runner([])(argv)

    with pytest.raises(launcher.LauncherError, match="swiftc"):
        launcher.ensure_app(failing, bundle_id="com.sam.chief-intake", command=["/other"], app_dir=tmp_path)
    assert plistlib.loads((first.app / "Contents/Info.plist").read_bytes())["ChiefIntakeCommand"] == COMMAND


def test_source_ships_and_only_accepts_preflight():
    source = launcher._source()
    assert 'case ["--preflight"]' in source and "ChiefIntakeCommand" in source
    assert "execv" not in source  # a child process keeps the app's permission identity
