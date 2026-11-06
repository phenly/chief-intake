# Support the prescribed pytest entry point in an uninstalled checkout.
import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import hashlib
import shutil
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from chief_intake.media import prepare, sha256_file, pdf_page_count, MediaError, MAX_BYTES
from chief_intake.models import Attachment


def photo(tmp_path, extension="jpg", uti="public.jpeg"):
    path = tmp_path / f"original.{extension}"
    path.write_bytes(b"synthetic image")
    return Attachment("photo", "photo", uti, path)


def fake_runner(width=2000, height=1000, code=0):
    calls = []

    def run(argv):
        calls.append(argv)
        if code:
            return CompletedProcess(argv, code, "", "synthetic error")
        if "pixelWidth" in argv:
            return CompletedProcess(argv, 0, f"file\n pixelWidth: {width}\n pixelHeight: {height}\n", "")
        source = argv[argv.index("--out") - 1]
        shutil.copyfile(source, argv[-1])
        return CompletedProcess(argv, 0, "", "")

    return run, calls


@pytest.mark.parametrize("extension,uti", [("heic", "public.heic"), ("HEIF", "public.heif"), ("png", "public.png")])
def test_conversion(tmp_path, extension, uti):
    attachment = photo(tmp_path, extension, uti)
    run, calls = fake_runner()
    parts = prepare([attachment], tmp_path / "work", run)
    assert calls == [["sips", "-s", "format", "jpeg", "-Z", "1568", str(attachment.path), "--out", str(parts[0].path)]]
    assert parts[0].media_type == "image/jpeg" and parts[0].path.suffix == ".jpg"
    assert attachment.path.read_bytes() == b"synthetic image"


@pytest.mark.parametrize("width,height,resize", [(1568, 700, False), (100, 200, False), (700, 1569, True)])
def test_jpeg_only_downsizes(tmp_path, width, height, resize):
    a = photo(tmp_path)
    run, calls = fake_runner(width, height)
    parts = prepare([a], tmp_path / "work", run)
    assert calls[0] == ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(a.path)]
    assert len(calls) == (2 if resize else 1)
    if resize:
        assert calls[1] == ["sips", "-Z", "1568", str(a.path), "--out", str(parts[0].path)]
    assert parts[0].path.read_bytes() == a.path.read_bytes()
    assert parts[0].path != a.path


def test_pdf_hash_and_pages(tmp_path):
    path = tmp_path / "scan.pdf"
    data = b"%PDF-1.4 /Type /Pages /Type /Page /Type\n/Page>> /Type /PageExtra"
    path.write_bytes(data)
    assert pdf_page_count(path) == 2
    assert sha256_file(path) == hashlib.sha256(data).hexdigest()
    a = Attachment("scan", "scan", "com.apple.paper.doc.scan", path)
    run, calls = fake_runner()
    parts = prepare([a], tmp_path / "work", run)
    assert not calls and parts[0].media_type == "application/pdf"
    assert parts[0].path.read_bytes() == data


def test_caps(tmp_path):
    a = photo(tmp_path)
    run, calls = fake_runner()
    with pytest.raises(MediaError, match="^too large$"):
        prepare([a] * 9, tmp_path / "work", run)
    assert calls == []
    pdf = tmp_path / "big.pdf"
    # Sparse synthetic file keeps fixture generation cheap.
    with pdf.open("wb") as stream:
        stream.truncate(MAX_BYTES + 1)
    with pytest.raises(MediaError, match="^too large$"):
        prepare([Attachment("scan", "scan", "com.apple.paper.doc.scan", pdf)], tmp_path / "work", run)


def test_total_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("chief_intake.media.MAX_BYTES", 20)
    a = photo(tmp_path)
    run, _ = fake_runner(100, 100)
    with pytest.raises(MediaError, match="too large"):
        prepare([a, a], tmp_path / "work", run)


@pytest.mark.parametrize("kind,path,problem,match", [("other", None, None, "unsupported"), ("photo", None, None, "not synced"), ("photo", None, "ambiguous media", "ambiguous")])
def test_unavailable_media(tmp_path, kind, path, problem, match):
    run, _ = fake_runner()
    with pytest.raises(MediaError, match=match):
        prepare([Attachment("a", kind, "public.jpeg", path, problem)], tmp_path / "work", run)


def test_sips_failure_and_invalid_dimensions(tmp_path):
    a = photo(tmp_path)
    run, _ = fake_runner(code=1)
    with pytest.raises(MediaError, match="sips failed"):
        prepare([a], tmp_path / "work", run)
    with pytest.raises(MediaError, match="dimensions"):
        prepare([a], tmp_path / "work", lambda args: CompletedProcess(args, 0, "pixelWidth: unknown", ""))


def test_empty(tmp_path):
    run, calls = fake_runner()
    assert prepare([], tmp_path / "work", run) == []
    assert not calls


def test_vault_copy_converts_heic_and_copies_others(tmp_path):
    from chief_intake.media import vault_copy, vault_suffix
    from subprocess import CompletedProcess
    calls = []
    def run(argv):
        calls.append(argv); Path(argv[-1]).write_bytes(b"jpeg"); return CompletedProcess(argv, 0, "", "")
    heic = tmp_path / "IMG.HEIC"; heic.write_bytes(b"heic")
    assert vault_suffix(heic) == ".heic"
    vault_copy(heic, tmp_path / "out.heic", run)
    assert (tmp_path / "out.heic").read_bytes() == b"heic"
    assert not calls
    vault_copy(heic, tmp_path / "out.jpg", run)
    assert calls and calls[0][:3] == ["sips", "-s", "format"]
    pdf = tmp_path / "a.pdf"; pdf.write_bytes(b"%PDF")
    vault_copy(pdf, tmp_path / "b.pdf", run)
    assert (tmp_path / "b.pdf").read_bytes() == b"%PDF" and len(calls) == 1


def test_safe_title_keeps_amounts():
    from chief_intake.render import safe_title
    assert safe_title("Dental statement, $24.00 past due") == "Dental statement 24.00 past due"
    assert safe_title("Tax $1,875.30 [[x]]") == "Tax 1,875.30 x"
