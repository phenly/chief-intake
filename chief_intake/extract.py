"""Structured Claude CLI extraction with injectable transport and escalation."""
from __future__ import annotations

import base64
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Callable

from jsonschema import Draft202012Validator, ValidationError

from .config import Config
from .models import (
    Extraction, ExtractionError, Extractor, IntakeNote, KINDS, MAIL_KINDS, MediaPart,
)

_RESOURCE_DIR = Path(__file__).parent
Runner = Callable[..., subprocess.CompletedProcess]


def build_schema(cfg: Config) -> dict[str, Any]:
    """Load a fresh schema so runtime enums never mutate a shared template."""
    schema = json.loads((_RESOURCE_DIR / "schema.json").read_text(encoding="utf-8"))
    schema["properties"]["owner"]["enum"] = list(cfg.owner_names)
    schema["properties"]["kind"]["enum"] = list(KINDS)
    return schema


def cli_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The CLI rejects a $schema dialect URI it can't resolve; send the schema body only."""
    return {k: v for k, v in schema.items() if k != "$schema"}


def _decode(data: Any, schema: dict[str, Any], model: str) -> Extraction:
    try:
        Draft202012Validator(schema).validate(data)
    except ValidationError as exc:
        field = ".".join(map(str, exc.absolute_path)) or "object"
        raise ExtractionError(f"schema violation at {field} ({exc.validator})") from None
    values = dict(data)
    for field in ("due_date", "penalty_date"):
        if values[field] is not None:
            try:
                values[field] = date.fromisoformat(values[field])
            except ValueError:
                raise ExtractionError(f"invalid calendar date in {field}") from None
    return Extraction(**values, model=model)


class ClaudeCLIExtractor:
    def __init__(
        self, cfg: Config, runner: Runner = subprocess.run,
        claude_bin: str = "claude", timeout: float = 180,
        work_dir: Path | str | None = None,
    ) -> None:
        self.cfg = cfg
        self.runner = runner
        self.claude_bin = claude_bin
        self.timeout = timeout
        # A fresh child per invocation keeps cwd empty, even when the caller's
        # work directory already holds prepared media or prior run artifacts.
        self.work_dir = Path(work_dir) if work_dir is not None else cfg.state_dir

    def extract(
        self, note: IntakeNote, media: list[MediaPart], today: date, model: str,
    ) -> Extraction:
        schema = build_schema(self.cfg)
        prompt = (_RESOURCE_DIR / "prompt.md").read_text(encoding="utf-8").format(
            today=today.isoformat(), user_name=self.cfg.user_name,
            owners="\n".join(f"{o.name}: {o.handles}" for o in self.cfg.owners),
            fallback_owner=self.cfg.fallback_owner,
        )
        content: list[dict[str, Any]] = []
        for block_type, mime in (("image", "image/jpeg"), ("document", "application/pdf")):
            for part in media:
                if part.media_type == mime:
                    content.append({"type": block_type, "source": {
                        "type": "base64", "media_type": mime,
                        "data": base64.b64encode(part.path.read_bytes()).decode("ascii"),
                    }})
        if any(p.media_type not in ("image/jpeg", "application/pdf") for p in media):
            raise ExtractionError("unsupported media type")
        content.append({"type": "text", "text": (
            f"{prompt}\n<<<NOTE\nTitle: {note.title}\n{note.text}\nNOTE>>>"
        )})
        message = {"type": "user", "message": {"role": "user", "content": content}}
        argv = [
            self.claude_bin, "-p", "--model", model, "--tools", "",
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--json-schema", json.dumps(cli_schema(schema)),
            "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands",
            "--settings", json.dumps({"disableAllHooks": True}),
        ]
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix="extract-", dir=self.work_dir) as cwd:
                result = self.runner(
                    argv, cwd=cwd, input=json.dumps(message) + "\n",
                    capture_output=True, text=True, timeout=self.timeout,
                )
        except subprocess.TimeoutExpired:
            raise ExtractionError("Claude CLI timeout") from None
        except (OSError, subprocess.CalledProcessError):
            raise ExtractionError("Claude CLI execution failed") from None
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()[:1]
            raise ExtractionError(
                f"Claude CLI nonzero exit ({result.returncode})"
                + (f": {detail[0][:200]}" if detail else "")
            )
        final = None
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                final = event
        if final is None:
            raise ExtractionError("missing result event")
        # Bound the diagnostic and exclude stdout / error payloads (item data).
        subtype = str(final.get("subtype", "unknown"))[:80]
        if final.get("is_error"):
            raise ExtractionError(f"Claude result error (subtype={subtype})")
        if "structured_output" not in final:
            raise ExtractionError(f"missing structured_output (subtype={subtype})")
        return _decode(final["structured_output"], schema, model)


def needs_escalation(extraction: Extraction) -> bool:
    return (
        not extraction.readable
        or (extraction.kind in MAIL_KINDS and not (extraction.sender or "").strip())
        or (extraction.kind in ("bill", "tax") and not (extraction.amount_due or "").strip())
    )


def extract_with_escalation(
    extractor: Extractor, note: IntakeNote, media: list[MediaPart], today: date, cfg: Config,
) -> Extraction:
    primary = extractor.extract(note, media, today, cfg.primary_model)
    if not needs_escalation(primary):
        return primary
    try:
        return extractor.extract(note, media, today, cfg.escalation_model)
    except ExtractionError:
        if primary.readable:
            return primary
        raise


def extraction_to_json(extraction: Extraction) -> str:
    data = asdict(extraction)
    for field in ("due_date", "penalty_date"):
        if data[field] is not None:
            data[field] = data[field].isoformat()
    return json.dumps(data)


def extraction_from_json(payload: str) -> Extraction:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        raise ExtractionError("invalid extraction JSON") from None
    if not isinstance(data, dict):
        raise ExtractionError("extraction JSON must be an object")
    data = dict(data)
    model = data.pop("model", "")
    if not isinstance(model, str):
        raise ExtractionError("invalid extraction model")
    schema = json.loads((_RESOURCE_DIR / "schema.json").read_text(encoding="utf-8"))
    schema["properties"]["kind"]["enum"] = list(KINDS)
    return _decode(data, schema, model)


class FakeExtractor:
    def __init__(
        self, answers: dict[str, Extraction | Exception],
        calls: list[tuple[IntakeNote, list[MediaPart], date, str]] | None = None,
    ) -> None:
        self.answers = answers
        self.calls = calls if calls is not None else []

    def extract(
        self, note: IntakeNote, media: list[MediaPart], today: date, model: str,
    ) -> Extraction:
        self.calls.append((note, media, today, model))
        answer = self.answers[note.note_id]
        if isinstance(answer, Exception):
            raise answer
        return answer
