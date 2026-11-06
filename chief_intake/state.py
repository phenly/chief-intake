"""Durable per-version checkpoints and single-run exclusion."""
from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import DONE, IN_PROGRESS, SUPERSEDED, IntakeNote, NoteRow

_LISTS = {"image_paths", "image_sha256"}
_DATES = {f.name for f in fields(NoteRow) if f.name.endswith("_at")} | {"note_modified"}
_COLUMNS = [f.name for f in fields(NoteRow)]


class AlreadyRunning(Exception):
    pass


class StateDB:
    def __init__(self, path: Path, *, read_only: bool = False):
        path = Path(path)
        if read_only:
            self.connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
            self.connection.row_factory = sqlite3.Row
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        definitions = ", ".join(f"{name} {'INTEGER' if name == 'attempts' else 'TEXT'}"
                                for name in _COLUMNS)
        with self.connection:
            self.connection.execute(f"CREATE TABLE IF NOT EXISTS notes ({definitions}, "
                                    "PRIMARY KEY (note_id, fingerprint))")
            self.connection.execute("CREATE TABLE IF NOT EXISTS runs ("
                                    "id INTEGER PRIMARY KEY, started TEXT NOT NULL, "
                                    "finished TEXT, counts TEXT, dry_run INTEGER NOT NULL DEFAULT 0)")

            self.connection.execute("CREATE TABLE IF NOT EXISTS cleanup_pending (identity TEXT PRIMARY KEY, record_path TEXT, message TEXT, report_path TEXT)")
            self.connection.execute("CREATE TABLE IF NOT EXISTS outputs (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, pending INTEGER NOT NULL, eligible INTEGER NOT NULL)")

    def pending_cleanup(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM cleanup_pending WHERE identity IN "
            "(SELECT note_id || ':' || fingerprint FROM notes WHERE images_expired_at IS NOT NULL)").fetchall()

    def remember_cleanup(self, row: NoteRow) -> None:
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO cleanup_pending VALUES (?, ?, ?, NULL)",
                                    (row.note_id + ':' + row.fingerprint, row.record_path,
                                     f'Images expired: {row.title}'))

    def remember_output(self, path: str, digest: str, *, eligible: bool = True) -> None:
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO outputs VALUES (?, ?, 1, ?)",
                                    (path, digest, int(eligible)))

    def output(self, path: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM outputs WHERE path=?", (path,)).fetchone()

    def pending_outputs(self) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM outputs WHERE pending=1").fetchall()

    def committed_outputs(self, paths: list[str]) -> None:
        with self.connection:
            self.connection.executemany("UPDATE outputs SET pending=0 WHERE path=?", [(p,) for p in paths])
            self.connection.execute("DELETE FROM cleanup_pending WHERE "
                                    "record_path IN (SELECT path FROM outputs WHERE pending=0) AND "
                                    "report_path IN (SELECT path FROM outputs WHERE pending=0) AND identity IN "
                                    "(SELECT note_id || ':' || fingerprint FROM notes WHERE images_expired_at IS NOT NULL)")

    def archive(self, row: NoteRow) -> None:
        # Preserve the prior occurrence's image ownership and retention clock.
        import uuid
        with self.connection:
            self.connection.execute("UPDATE notes SET fingerprint=?, status=? WHERE note_id=? AND fingerprint=?",
                                    (row.fingerprint + ':history:' + uuid.uuid4().hex, SUPERSEDED,
                                     row.note_id, row.fingerprint))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> StateDB:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @staticmethod
    def _row(raw: sqlite3.Row | None) -> NoteRow | None:
        if raw is None:
            return None
        data = dict(raw)
        for key in _LISTS:
            data[key] = json.loads(data[key])
        for key in _DATES:
            if data[key] is not None:
                data[key] = datetime.fromisoformat(data[key])
        return NoteRow(**data)

    def get(self, note_id: str, fingerprint: str) -> NoteRow | None:
        return self._row(self.connection.execute(
            "SELECT * FROM notes WHERE note_id=? AND fingerprint=?", (note_id, fingerprint)).fetchone())

    def latest(self, note_id: str) -> NoteRow | None:
        return self._row(self.connection.execute(
            "SELECT * FROM notes WHERE note_id=? ORDER BY note_modified DESC, rowid DESC LIMIT 1",
            (note_id,)).fetchone())

    def upsert(self, row: NoteRow) -> None:
        values = []
        for name in _COLUMNS:
            value = getattr(row, name)
            if name in _LISTS:
                value = json.dumps(value)
            elif name in _DATES and value is not None:
                value = value.isoformat()
            values.append(value)
        updates = ", ".join(f"{name}=excluded.{name}" for name in _COLUMNS
                            if name not in {"note_id", "fingerprint"})
        with self.connection:
            self.connection.execute(
                f"INSERT INTO notes ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)}) "
                f"ON CONFLICT(note_id, fingerprint) DO UPDATE SET {updates}", values)

    def rows_with_status(self, *statuses: str) -> list[NoteRow]:
        if not statuses:
            return []
        raw = self.connection.execute(
            f"SELECT * FROM notes WHERE status IN ({', '.join('?' for _ in statuses)}) ORDER BY rowid",
            statuses)
        return [self._row(row) for row in raw]

    def incomplete(self) -> list[NoteRow]:
        return self.rows_with_status(IN_PROGRESS)

    def uncommitted_done(self) -> list[NoteRow]:
        return [row for row in self.rows_with_status(DONE) if row.stage_commit_at is None]

    def supersede_older(self, note_id: str, fingerprint: str) -> None:
        with self.connection:
            # A caller processes the new version before superseding prior versions.
            # Never relabel an already deleted version.
            self.connection.execute(
                "UPDATE notes SET status=? WHERE note_id=? AND fingerprint<>? AND status<>'deleted' "
                "AND note_modified <= (SELECT note_modified FROM notes WHERE note_id=? AND fingerprint=?)",
                (SUPERSEDED, note_id, fingerprint, note_id, fingerprint))

    def start_run(self) -> int:
        with self.connection:
            cursor = self.connection.execute("INSERT INTO runs (started) VALUES (?)",
                                             (datetime.now(timezone.utc).isoformat(),))
        return cursor.lastrowid

    def finish_run(self, id: int, counts: dict, dry_run: bool) -> None:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE runs SET finished=?, counts=?, dry_run=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), json.dumps(counts), int(dry_run), id))
            if cursor.rowcount != 1:
                raise ValueError(f"unknown run: {id}")


@contextmanager
def lock(state_dir: Path) -> Iterator[None]:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "run.lock").open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("already running") from exc
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def fingerprint(note: IntakeNote) -> str:
    digest = hashlib.sha256()
    def add(value: str) -> None:
        digest.update(value.encode("utf-8"))
    add(note.text)
    for attachment in note.attachments:
        add(attachment.uuid)
        if attachment.path is not None and attachment.path.is_file():
            media = hashlib.sha256()
            with attachment.path.open("rb") as file:
                for block in iter(lambda: file.read(1024 * 1024), b""):
                    media.update(block)
            add(media.hexdigest())
        else:
            add("missing")
    return digest.hexdigest()
