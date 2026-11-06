"""Prepare bounded model inputs without changing the originals."""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from subprocess import CompletedProcess
from typing import Callable, Iterable

from .models import Attachment, MediaPart, ATTACHMENT_OTHER

Runner = Callable[[list[str]], CompletedProcess]
MAX_PARTS = 8
MAX_BYTES = 20 * 1024 * 1024


class MediaError(Exception):
    pass


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_page_count(path: str | Path) -> int:
    """Count plain /Type /Page objects, excluding /Pages.

    This lightweight estimate cannot inspect compressed object streams, encrypted
    PDFs or distinguish page-like text inside streams; it is not a PDF validator.
    """
    return len(re.findall(rb"/Type\s*/Page(?=[\s/<>\[\]()%]|$)", Path(path).read_bytes()))


def prepare(attachments: Iterable[Attachment], work_dir: Path, runner: Runner) -> list[MediaPart]:
    items = list(attachments)
    if len(items) > MAX_PARTS:
        raise MediaError("too large")
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    total = 0

    def run(argv: list[str]) -> str:
        result = runner(argv)
        if result.returncode:
            raise MediaError(f"sips failed: {result.stderr}")
        return result.stdout

    for index, attachment in enumerate(items, 1):
        if attachment.kind == ATTACHMENT_OTHER:
            raise MediaError("unsupported attachment")
        if attachment.problem or attachment.path is None:
            raise MediaError(attachment.problem or "media not synced")
        source = Path(attachment.path).absolute()
        suffix = source.suffix.lower()
        is_pdf = attachment.kind == "scan" or attachment.type_uti == "com.adobe.pdf" or suffix == ".pdf"
        destination = work_dir.absolute() / f"media-{index}{'.pdf' if is_pdf else '.jpg'}"
        if source == destination:
            raise MediaError("work output would overwrite source")
        if is_pdf:
            shutil.copy2(source, destination)
            media_type = "application/pdf"
        elif suffix in {".heic", ".heif", ".png"} or attachment.type_uti in {"public.heic", "public.heif", "public.png"}:
            run(["sips", "-s", "format", "jpeg", "-Z", "1568", str(source), "--out", str(destination)])
            media_type = "image/jpeg"
        elif suffix in {".jpg", ".jpeg"} or attachment.type_uti == "public.jpeg":
            dimensions = run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(source)])
            sizes = dict(re.findall(r"\b(pixelWidth|pixelHeight):\s*(\d+)", dimensions))
            if set(sizes) != {"pixelWidth", "pixelHeight"} or min(map(int, sizes.values())) <= 0:
                raise MediaError("cannot read image dimensions")
            if max(map(int, sizes.values())) > 1568:
                run(["sips", "-Z", "1568", str(source), "--out", str(destination)])
            else:
                shutil.copy2(source, destination)
            media_type = "image/jpeg"
        else:
            raise MediaError("unsupported attachment")
        if not destination.is_file():
            raise MediaError("media preparation produced no file")
        total += destination.stat().st_size
        if total > MAX_BYTES:
            raise MediaError("too large")
        parts.append(MediaPart(destination, media_type))
    return parts


VIEWABLE_CONVERT = {".heic", ".heif"}


def vault_suffix(path: Path) -> str:
    """Original suffix retained in the vault."""
    suffix = path.suffix.lower()
    return suffix


def vault_copy(source: Path, target: Path, runner: Runner) -> None:
    """Copy original bytes, or explicitly create a JPEG viewing derivative."""
    if source.suffix.lower() in VIEWABLE_CONVERT and target.suffix.lower() == ".jpg":
        result = runner(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "85",
                         str(source), "--out", str(target)])
        if result.returncode != 0 or not target.exists():
            raise MediaError(f"sips failed: {result.stderr}")
    else:
        shutil.copy2(source, target)
