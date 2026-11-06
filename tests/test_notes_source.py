# Support the prescribed pytest entry point in an uninstalled checkout.
import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import json
import os
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from apple_notes_parser.database import AppleNotesDatabase, ProtobufParser
from apple_notes_parser.models import Account, Folder

from chief_intake.config import Config
from chief_intake.notes_source import ParserSource, FixtureSource, NotesSourceError, resolve_attachment

NOW = datetime(2026, 11, 6, 12)


def cfg(tmp_path, **kwargs):
    return Config(notes_folder="intake", vault=tmp_path, inbox_dir="Inbox", board="board.md",
                  owners=(), fallback_owner="Sam", notes_container=tmp_path, **kwargs)


def put(path, data=b"photo", modified=NOW):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (modified.timestamp(), modified.timestamp()))
    return path


def test_exact_photo_resolution(tmp_path):
    # Photos live under Media/<media id (ZMEDIA row)>/<generation>/<filename>.
    target = put(tmp_path / "Accounts/account/Media/media1/version/image.jpg")
    put(tmp_path / "Accounts/other/Media/media1/version/image.jpg")
    put(tmp_path / "Accounts/account/Media/photo/version/image.jpg")  # attachment uuid dir: never used
    put(target.parent / ".hidden")
    put(target.parent / ".hidden-dir/image.jpg")
    put(target.parent / "thumbnail.jpg")  # other filename in the same dir: ignored
    a = resolve_attachment(tmp_path, "account", "photo", "public.jpeg", NOW, "media1", "image.jpg")
    assert a.path == target and a.problem is None and a.kind == "photo"
    assert resolve_attachment(tmp_path, "account", "photo", "public.jpeg", NOW).path is None
    put(tmp_path / "Accounts/account/Media/media1/version2/image.jpg")
    a = resolve_attachment(tmp_path, "account", "photo", "public.jpeg", NOW, "media1", "image.jpg")
    assert a.path is None and a.problem == "ambiguous media"


def test_unsynced_no_filename_fallback(tmp_path):
    put(tmp_path / "Accounts/account/Media/other/version/image.jpg")
    assert resolve_attachment(tmp_path, "account", "missing", "public.jpeg", NOW).path is None
    assert resolve_attachment(tmp_path, "account", "", "public.jpeg", NOW).path is None
    assert resolve_attachment(tmp_path, "../account", "x", "public.jpeg", NOW).path is None


def test_scan_resolution(tmp_path):
    root = tmp_path / "Accounts/account/FallbackPDFs/scan"
    old = put(root / "v1/FallbackPDF.pdf", b"%PDF-1.4 /Type /Page /Type /Pages", NOW - timedelta(seconds=1))
    a = resolve_attachment(tmp_path, "account", "scan", "com.apple.paper.doc.scan", NOW)
    assert a.path is None and a.problem == "scan older than note"
    new = put(root / "v2/FallbackPDF.pdf", old.read_bytes())
    assert resolve_attachment(tmp_path, "account", "scan", "com.apple.paper.doc.scan", NOW).path == new
    new.write_bytes(b"not PDF /Type /Page")
    assert resolve_attachment(tmp_path, "account", "scan", "com.apple.paper.doc.scan", NOW).problem == "invalid scan PDF"
    new.write_bytes(b"%PDF-1.4 /Type /Pages")
    assert resolve_attachment(tmp_path, "account", "scan", "com.apple.paper.doc.scan", NOW).path is None


def test_unsupported(tmp_path):
    a = resolve_attachment(tmp_path, "account", "audio", "public.mp3", NOW)
    assert a.kind == "other" and a.path is None


def fake_database(monkeypatch, accounts, folders, notes=()):
    db = MagicMock()
    db.__enter__.return_value = db
    db.get_accounts.return_value = accounts
    db.get_folders.return_value = folders
    db.get_notes.return_value = notes
    constructor = MagicMock(return_value=db)
    monkeypatch.setattr("chief_intake.notes_source.AppleNotesDatabase", constructor)
    return db, constructor


def test_folder_selection_and_mapping(tmp_path, monkeypatch):
    a = Account(1, "Cloud", "account")
    b = Account(2, "Local", "other")
    folder = Folder(4, "intake", a)
    photo = put(tmp_path / "Accounts/account/Media/media1/v/image.jpg")
    note = NS(applescript_id="x-coredata://synthetic/ICNote/p7", account=a,
              title="Synthetic idea", content="Sample text", creation_date=NOW, modification_date=NOW,
              is_password_protected=False,
              attachments=[NS(uuid="photo", type_uti="public.jpeg"),
                           NS(uuid="tag", type_uti="com.apple.notes.inlinetextattachment.hashtag"),
                           NS(uuid="drawing", type_uti="com.apple.drawing.2")])
    db, constructor = fake_database(monkeypatch, [a, b], [folder, Folder(5, "Elsewhere", b)], [note])
    db.connection.execute.return_value.fetchone.return_value = ("media1", "image.jpg")
    result = ParserSource(cfg(tmp_path, notes_account="Cloud")).list_notes()
    constructor.assert_called_once_with(str(tmp_path / "NoteStore.sqlite"))
    db.get_folders.assert_called_once_with({1: a})
    db.get_notes.assert_called_once_with({1: a}, {4: folder})
    assert result[0].note_id == note.applescript_id
    assert result[0].text == "Sample text" and result[0].account_uuid == "account"
    assert result[0].attachments[0].path == photo
    assert len(result[0].attachments) == 2 and result[0].attachments[1].kind == "other"


@pytest.mark.parametrize("names,reason", [([], "not found"), (["intake", "intake"], "ambiguous")])
def test_folder_errors(tmp_path, monkeypatch, names, reason):
    a = Account(1, "Cloud", "account")
    db, _ = fake_database(monkeypatch, [a], [Folder(i, n, a) for i, n in enumerate(names)])
    with pytest.raises(NotesSourceError, match=reason):
        ParserSource(cfg(tmp_path)).list_notes()
    db.get_notes.assert_not_called()


def test_installed_parser_decodes_only_target_rows(monkeypatch):
    """Exercise the real dependency get_notes; fail if an outside blob is decoded."""
    connection = sqlite3.connect(":memory:")
    connection.executescript('''
        CREATE TABLE ZICNOTEDATA (Z_PK, ZNOTE, ZDATA);
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE1, ZCREATIONDATE3,
          ZMODIFICATIONDATE1, ZACCOUNT7, ZFOLDER, ZISPINNED, ZIDENTIFIER, ZISPASSWORDPROTECTED);
        INSERT INTO ZICNOTEDATA VALUES (1, 10, X'746172676574'), (2, 20, X'6f74686572'), (3, 30, X'6f74686572');
        INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (10, 'Sample', 800000000, 800000000, 1, 4, 0, 'a', 0),
          (20, 'Outside folder', 800000000, 800000000, 1, 5, 0, 'b', 0),
          (30, 'Outside account', 800000000, 800000000, 2, 4, 0, 'c', 0);
    ''')
    db = AppleNotesDatabase.__new__(AppleNotesDatabase)
    db.connection = connection
    db._macos_version = 12
    db._embedded_extractor = None
    monkeypatch.setattr(db, "get_z_uuid", lambda: "synthetic")
    monkeypatch.setattr(db, "get_attachments", lambda accounts: [])
    seen = []

    def decode(blob):
        assert blob == b"target"
        seen.append(blob)
        return "Sample text"

    monkeypatch.setattr(ProtobufParser, "extract_note_text", decode)
    monkeypatch.setattr(ProtobufParser, "parse_note_structure", lambda blob: {} if blob == b"target" else pytest.fail("outside decode"))
    a = Account(1, "Cloud", "account")
    try:
        assert len(db.get_notes({1: a}, {4: Folder(4, "intake", a)})) == 1
        assert seen == [b"target"]
    finally:
        connection.close()


def test_fixture_source(tmp_path):
    row = dict(note_id="synthetic", account_uuid="account", title="Idea", text="Sample",
               created=NOW.isoformat(), modified=NOW.isoformat(), attachments=[
                   dict(uuid="photo", kind="photo", type_uti="public.jpeg", file="image.jpg"),
                   dict(uuid="scan", kind="scan", type_uti="com.apple.paper.doc.scan", file=None, problem="scan older than note")])
    fixture = tmp_path / "notes.jsonl"
    fixture.write_text(json.dumps(row) + "\n\n")
    note = FixtureSource(fixture, tmp_path).list_notes()[0]
    assert note.created == NOW and note.attachments[0].path == tmp_path / "image.jpg"
    assert note.attachments[1].problem == "scan older than note"
    row["attachments"][0]["file"] = "../escape.jpg"
    fixture.write_text(json.dumps(row))
    with pytest.raises(NotesSourceError, match="relative"):
        FixtureSource(fixture, tmp_path).list_notes()


def test_locked_note_needs_attention(tmp_path, monkeypatch):
    a = Account(1, "Cloud", "account")
    folder = Folder(4, "intake", a)
    note = NS(applescript_id="x-coredata://synthetic/ICNote/p8", account=a, title="Locked",
              content="", creation_date=NOW, modification_date=NOW, is_password_protected=True,
              attachments=[])
    fake_database(monkeypatch, [a], [folder], [note])
    att = ParserSource(cfg(tmp_path)).list_notes()[0].attachments
    assert att[0].kind == "other" and att[0].problem == "password-protected note"
