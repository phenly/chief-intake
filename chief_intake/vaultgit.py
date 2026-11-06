"""Explicit-path git operations; intake binaries are never staged."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

Runner = Callable[[list[str]], subprocess.CompletedProcess]
BINARY_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".pdf", ".gif", ".webp",
                     ".tif", ".tiff", ".bmp", ".svg", ".avif", ".ico"}


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


def _error(result: subprocess.CompletedProcess) -> str:
    output = result.stderr or result.stdout or f"git exited {result.returncode}"
    return output.decode(errors="replace").strip() if isinstance(output, bytes) else output.strip()


def ensure_ignored(gitignore_path: Path, patterns: list[str]) -> bool:
    path = Path(gitignore_path)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    existing = set(original.splitlines())
    missing = list(dict.fromkeys(p for p in patterns if p not in existing))
    if not missing:
        return False
    if any("\n" in p or "\r" in p for p in missing):
        raise ValueError("ignore patterns must be single lines")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(("\n" if original and not original.endswith("\n") else "")
                   + "\n".join(missing) + "\n")
    return True


def intake_ignore_patterns(inbox_dir: str | Path) -> list[str]:
    directory = Path(inbox_dir).as_posix().rstrip("/")
    if Path(inbox_dir).is_absolute() or ".." in Path(inbox_dir).parts:
        raise ValueError("inbox directory must be vault-relative")
    # Escape gitignore metacharacters in a configured literal directory.
    directory = directory.replace("\\", "\\\\")
    for character in "*?[]#!":
        directory = directory.replace(character, "\\" + character)
    prefix = "" if directory == "." else directory + "/"
    return [f"{prefix}Intake *.{case}" for ext in ("jpg", "jpeg", "png", "heic", "heif", "pdf")
            for case in (ext, ext.upper())]


class GitVault:
    def __init__(self, vault: Path, runner: Runner = _default_runner):
        self.vault = Path(vault)
        self.runner = runner

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        # check-ignore consumes literal filenames and rejects pathspec magic.
        options = [] if args[0] == "check-ignore" else ["--literal-pathspecs"]
        return self.runner(["git", "-C", str(self.vault), *options, *args])

    def _paths(self, paths: list[str | Path]) -> list[str]:
        result = []
        for path in paths:
            absolute = (self.vault / path).resolve()
            try:
                relative = absolute.relative_to(self.vault.resolve())
            except ValueError as exc:
                raise ValueError("git path must be inside vault") from exc
            if relative == Path("."):
                raise ValueError("explicit file paths required")
            result.append(relative.as_posix())
        return list(dict.fromkeys(result))

    def is_repo(self) -> bool:
        result = self._run("rev-parse", "--is-inside-work-tree")
        output = result.stdout
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return result.returncode == 0 and (output or "").strip() == "true"

    def dirty_vs_head(self, path: str | Path) -> bool:
        result = self._run("diff", "--quiet", "HEAD", "--", *self._paths([path]))
        if result.returncode not in (0, 1):
            raise RuntimeError(_error(result))
        if result.returncode == 1:
            return True
        # diff does not see untracked files; an existing untracked shared file
        # contains somebody else's bytes too.
        names = self._paths([path])
        if (self.vault / names[0]).exists():
            tracked = self._run('ls-files', '--error-unmatch', '--', *names)
            if tracked.returncode not in (0, 1):
                raise RuntimeError(_error(tracked))
            return tracked.returncode == 1
        return False

    def ensure_ignored(self, gitignore_path: Path, patterns: list[str]) -> bool:
        path = Path(gitignore_path)
        return ensure_ignored(path if path.is_absolute() else self.vault / path, patterns)

    def check_ignored(self, paths: list[str | Path]) -> bool:
        names = self._paths(paths)
        if not names:
            return True
        # One query per path proves that *every* file is ignored.
        for name in names:
            result = self._run("check-ignore", "--quiet", "--", name)
            if result.returncode == 1:
                return False
            if result.returncode != 0:
                raise RuntimeError(_error(result))
        return True

    def commit(self, paths: list[str | Path], message: str) -> str:
        names = self._paths(paths)
        if any(Path(name).suffix.lower() in BINARY_EXTENSIONS for name in names):
            raise ValueError("refusing to stage an image or PDF")
        # Also refuse directories, which could implicitly stage binary children.
        if any((self.vault / name).is_dir() for name in names):
            raise ValueError("explicit file paths required")
        if not names:
            return "nothing"
        result = self._run("add", "--", *names)
        if result.returncode:
            return _error(result)
        result = self._run("diff", "--cached", "--quiet", "--", *names)
        if result.returncode == 0:
            return "nothing"
        if result.returncode != 1:
            return _error(result)
        result = self._run("commit", "--only", "-m", message, "--", *names)
        return "committed" if result.returncode == 0 else _error(result)
