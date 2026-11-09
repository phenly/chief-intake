# Support the prescribed pytest entry point in an uninstalled checkout.
import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import plistlib
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from chief_intake.config import Config
from chief_intake import schedule


def cfg(tmp_path):
    return Config(notes_folder="intake", vault=tmp_path, inbox_dir="Inbox", board="board.md",
                  owners=(), fallback_owner="Sam", state_dir=tmp_path / "state")


def fake_runner(failure=None):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[0] == "swiftc":
            Path(argv[argv.index("-o") + 1]).write_text("synthetic binary")
        # Bootout failure is explicitly tolerated.
        code = 1 if "bootout" in argv or (failure and failure in argv) else 0
        return CompletedProcess(argv, code, "", "synthetic failure" if code else "")

    return run, calls


def launchctl(calls):
    return [c for c in calls if c[0] in ("plutil", "launchctl")]


def test_render(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.getpass, "getuser", lambda: "sam")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    exe = tmp_path / "Apps with spaces/Chief Intake.app/Contents/MacOS/chief-intake-launcher"
    payload = plistlib.loads(schedule.render_plist(cfg(tmp_path), exe).encode())
    assert payload["Label"] == "com.sam.chief-intake"
    assert payload["ProgramArguments"] == [str(exe)]
    assert payload["StartCalendarInterval"] == {"Hour": 7, "Minute": 0}
    assert payload["EnvironmentVariables"]["PATH"].split(":") == [str(tmp_path / ".local/bin"), "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    assert payload["StandardOutPath"] == payload["StandardErrorPath"] == str(tmp_path / "state/launchd.log")
    assert "RunAtLoad" not in payload
    assert plistlib.loads(schedule.render_plist(cfg(tmp_path), exe, "custom", 23).encode())["StartCalendarInterval"]["Hour"] == 23
    preflight = plistlib.loads(schedule.render_plist(cfg(tmp_path), exe, preflight=True).encode())
    assert preflight["ProgramArguments"] == [str(exe), "--preflight"]
    assert "StartCalendarInterval" not in preflight
    with pytest.raises(ValueError):
        schedule.render_plist(cfg(tmp_path), exe, hour=24)


def test_install_remove(tmp_path):
    run, calls = fake_runner()
    directory = tmp_path / "LaunchAgents"
    installed = schedule.install(cfg(tmp_path), run, python_path=tmp_path / "venv/bin/python",
                                 label="com.sam.chief-intake", launch_agents_dir=directory, uid=501,
                                 app_dir=tmp_path / "Applications")
    path = installed.plist
    assert path == directory / "com.sam.chief-intake.plist"
    payload = plistlib.loads(path.read_bytes())
    assert payload["Label"] == "com.sam.chief-intake"
    assert payload["ProgramArguments"] == [str(installed.app.executable)]
    assert installed.app.app == tmp_path / "Applications/Chief Intake.app" and installed.app.rebuilt
    info = plistlib.loads((installed.app.app / "Contents/Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "com.sam.chief-intake"
    assert info["ChiefIntakeCommand"][:4] == [str(tmp_path / "venv/bin/python"), "-m", "chief_intake", "run"]
    assert launchctl(calls) == [["plutil", "-lint", str(path)],
                                ["launchctl", "bootout", "gui/501/com.sam.chief-intake"],
                                ["launchctl", "bootstrap", "gui/501", str(path)]]
    assert cfg(tmp_path).state_dir.is_dir()
    schedule.remove(cfg(tmp_path), run, label="com.sam.chief-intake", launch_agents_dir=directory, uid=501)
    assert not path.exists()
    assert installed.app.app.exists()  # keeps the user's permission grants
    assert calls[-1] == ["launchctl", "bootout", "gui/501/com.sam.chief-intake"]
    schedule.remove(cfg(tmp_path), run, label="com.sam.chief-intake", launch_agents_dir=directory, uid=501)


def test_reinstall_keeps_app_identity(tmp_path):
    run, calls = fake_runner()
    kwargs = dict(python_path=tmp_path / "venv/bin/python", label="com.sam.chief-intake",
                  launch_agents_dir=tmp_path / "LaunchAgents", uid=501, app_dir=tmp_path / "Applications")
    schedule.install(cfg(tmp_path), run, **kwargs)
    calls.clear()
    again = schedule.install(cfg(tmp_path), run, **kwargs)
    assert not again.app.rebuilt
    assert not [c for c in calls if c[0] == "swiftc"]


@pytest.mark.parametrize("failure", ["plutil", "bootstrap", "swiftc", "--sign"])
def test_install_failure(tmp_path, failure):
    run, calls = fake_runner(failure)
    with pytest.raises(schedule.ScheduleError):
        schedule.install(cfg(tmp_path), run, launch_agents_dir=tmp_path / "LaunchAgents",
                         app_dir=tmp_path / "Applications")
    if failure in ("swiftc", "--sign"):
        assert not launchctl(calls)
        assert not (tmp_path / "Applications/Chief Intake.app").exists()


def test_preflight_flag_before_kickstart(tmp_path):
    calls = []
    inner, _ = fake_runner()

    def run(argv):
        if argv[0] == "launchctl":
            payload = plistlib.loads((cfg(tmp_path).state_dir / "com.sam.chief-intake.preflight.plist").read_bytes())
            assert payload["ProgramArguments"][-1] == "--preflight"
            assert payload["ProgramArguments"][0].endswith("Chief Intake.app/Contents/MacOS/chief-intake-launcher")
            assert 'StartCalendarInterval' not in payload
        calls.append(argv)
        return inner(argv) if argv[0] != "launchctl" else CompletedProcess(argv, 0, "", "")

    app = schedule.test_kickstart(cfg(tmp_path), run, label="com.sam.chief-intake", uid=501,
                                  app_dir=tmp_path / "Applications")
    assert calls[-1] == ["launchctl", "kickstart", "-k", "gui/501/com.sam.chief-intake.preflight"]
    info = plistlib.loads((app.app / "Contents/Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "com.sam.chief-intake"  # same app as the daily job
    assert not (tmp_path / "Library").exists()


def test_preflight_failure(tmp_path):
    run, _ = fake_runner("kickstart")
    with pytest.raises(schedule.ScheduleError):
        schedule.test_kickstart(cfg(tmp_path), run, app_dir=tmp_path / "Applications")


def test_setup_instructions_name_the_app(tmp_path):
    app = schedule.launcher.AppResult(tmp_path / "Chief Intake.app", rebuilt=True)
    text = schedule.print_setup_instructions(app)
    assert str(app.app) in text and "Full Disk Access" in text and '"Chief Intake"' in text
    assert "schedule test" in text and "python3" not in text
    unchanged = schedule.print_setup_instructions(schedule.launcher.AppResult(app.app, rebuilt=False))
    assert "unchanged" in unchanged and "turn on" not in unchanged


def test_label_cannot_escape_directory(tmp_path):
    run, calls = fake_runner()
    with pytest.raises(ValueError):
        schedule.install(cfg(tmp_path), run, label="../escape", launch_agents_dir=tmp_path,
                         app_dir=tmp_path / "Applications")
    assert not calls


def test_custom_config_is_absolute_in_installed_job(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    selected = Path('custom config.toml')
    selected.write_text('# synthetic')
    monkeypatch.setenv('CHIEF_INTAKE_CONFIG', str(selected))
    run, _ = fake_runner()
    installed = schedule.install(cfg(tmp_path), run, launch_agents_dir=tmp_path / 'LaunchAgents',
                                 app_dir=tmp_path / 'Applications')
    args = plistlib.loads((installed.app.app / 'Contents/Info.plist').read_bytes())['ChiefIntakeCommand']
    assert args[args.index('--config') + 1] == str(selected.resolve())
    assert args[-2:] == ['--config', str(selected.resolve())]
