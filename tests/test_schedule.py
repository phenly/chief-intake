# Support the prescribed pytest entry point in an uninstalled checkout.
import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import os
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
        # Bootout failure is explicitly tolerated.
        code = 1 if "bootout" in argv or (failure and failure in argv) else 0
        return CompletedProcess(argv, code, "", "synthetic failure" if code else "")

    return run, calls


def test_render(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule.getpass, "getuser", lambda: "sam")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    python = tmp_path / "venv with spaces/bin/python"
    payload = plistlib.loads(schedule.render_plist(cfg(tmp_path), python).encode())
    assert payload["Label"] == "com.sam.chief-intake"
    assert payload["ProgramArguments"][:4] == [str(python), "-m", "chief_intake", "run"]
    assert payload["ProgramArguments"][-2:] == ["--trigger", "launchd"]
    assert payload["StartCalendarInterval"] == {"Hour": 7, "Minute": 0}
    assert payload["EnvironmentVariables"]["PATH"].split(":") == [str(tmp_path / ".local/bin"), "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    assert payload["StandardOutPath"] == payload["StandardErrorPath"] == str(tmp_path / "state/launchd.log")
    assert "RunAtLoad" not in payload
    assert plistlib.loads(schedule.render_plist(cfg(tmp_path), python, "custom", 23).encode())["StartCalendarInterval"]["Hour"] == 23
    with pytest.raises(ValueError):
        schedule.render_plist(cfg(tmp_path), python, hour=24)


def test_install_remove(tmp_path):
    run, calls = fake_runner()
    directory = tmp_path / "LaunchAgents"
    path = schedule.install(cfg(tmp_path), run, python_path=tmp_path / "venv/bin/python",
                            label="com.sam.chief-intake", launch_agents_dir=directory, uid=501)
    assert path == directory / "com.sam.chief-intake.plist"
    assert plistlib.loads(path.read_bytes())["Label"] == "com.sam.chief-intake"
    assert calls == [["plutil", "-lint", str(path)],
                     ["launchctl", "bootout", "gui/501/com.sam.chief-intake"],
                     ["launchctl", "bootstrap", "gui/501", str(path)]]
    assert cfg(tmp_path).state_dir.is_dir()
    schedule.remove(cfg(tmp_path), run, label="com.sam.chief-intake", launch_agents_dir=directory, uid=501)
    assert not path.exists()
    assert calls[-1] == ["launchctl", "bootout", "gui/501/com.sam.chief-intake"]
    schedule.remove(cfg(tmp_path), run, label="com.sam.chief-intake", launch_agents_dir=directory, uid=501)


@pytest.mark.parametrize("failure", ["plutil", "bootstrap"])
def test_install_failure(tmp_path, failure):
    run, calls = fake_runner(failure)
    with pytest.raises(schedule.ScheduleError):
        schedule.install(cfg(tmp_path), run, launch_agents_dir=tmp_path / "LaunchAgents")
    if failure == "plutil":
        assert len(calls) == 1


def test_preflight_flag_before_kickstart(tmp_path):
    calls = []

    def run(argv):
        payload = plistlib.loads((cfg(tmp_path).state_dir / "com.sam.chief-intake.preflight.plist").read_bytes())
        assert '--preflight' in payload['ProgramArguments']
        assert 'StartCalendarInterval' not in payload
        calls.append(argv)
        return CompletedProcess(argv, 0, "", "")

    schedule.test_kickstart(cfg(tmp_path), run, label="com.sam.chief-intake", uid=501)
    assert calls[-1] == ["launchctl", "kickstart", "-k", "gui/501/com.sam.chief-intake.preflight"]
    assert not (tmp_path / "Library").exists()


def test_preflight_failure(tmp_path):
    run, _ = fake_runner("kickstart")
    with pytest.raises(schedule.ScheduleError):
        schedule.test_kickstart(cfg(tmp_path), run)


def test_setup_resolves_binary(tmp_path):
    binary = tmp_path / "python-real"
    binary.touch()
    link = tmp_path / "python"
    link.symlink_to(binary)
    text = schedule.print_setup_instructions(link)
    assert os.path.realpath(link) in text and "Full Disk Access" in text
    assert "schedule test" in text and "upgrade" in text


def test_label_cannot_escape_directory(tmp_path):
    run, calls = fake_runner()
    with pytest.raises(ValueError):
        schedule.install(cfg(tmp_path), run, label="../escape", launch_agents_dir=tmp_path)
    assert not calls


def test_custom_config_is_absolute_in_installed_job(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    selected = Path('custom config.toml')
    selected.write_text('# synthetic')
    monkeypatch.setenv('CHIEF_INTAKE_CONFIG', str(selected))
    run, _ = fake_runner()
    path = schedule.install(cfg(tmp_path), run,
                            launch_agents_dir=tmp_path / 'LaunchAgents')
    args = plistlib.loads(path.read_bytes())['ProgramArguments']
    assert args[args.index('--config') + 1] == str(selected.resolve())
    assert args[-2:] == ['--trigger', 'launchd']
