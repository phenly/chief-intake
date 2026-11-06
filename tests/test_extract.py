"""Synthetic extraction transport and policy tests; never invoke the real CLI."""
import base64
from dataclasses import replace
from datetime import date, datetime
import json
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

# Support the prescribed pytest entry point in an uninstalled source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chief_intake.config import Config, Owner
from chief_intake.extract import (
    cli_schema,
    ClaudeCLIExtractor, FakeExtractor, build_schema, extraction_from_json,
    extraction_to_json, extract_with_escalation, needs_escalation,
)
from chief_intake.models import Extraction, ExtractionError, IntakeNote, KINDS, MediaPart

TODAY = date(2026, 11, 6)


@pytest.fixture
def cfg(tmp_path):
    return Config(
        notes_folder="Synthetic Intake", vault=tmp_path / "vault", inbox_dir="Inbox",
        board="Board.md", owners=(Owner("Sam", "household bills"), Owner("Alex", "ideas")),
        fallback_owner="Alex", user_name="Sam", state_dir=tmp_path / "state",
    )


@pytest.fixture
def note():
    return IntakeNote("synthetic-note", "synthetic-account", "Sample utility bill",
                      "I think escrow covers this.", datetime(2026, 11, 1), datetime(2026, 11, 1))


@pytest.fixture
def answer():
    return Extraction(True, "bill", "Sample utility bill", "A synthetic bill.", "Sam",
                      "Check the mortgage escrow statements.", sender="Sample Utility",
                      amount_due="$10.00", due_date=date(2026, 11, 20))


def output(answer):
    data = json.loads(extraction_to_json(answer))
    data.pop("model")
    return data


def event(data, **kwargs):
    return json.dumps({"type": "result", "subtype": "success", "structured_output": data, **kwargs})


def runner_for(stdout, returncode=0):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout, "sensitive stderr omitted")
    return run


def test_schema_is_complete_and_independent(cfg, answer):
    schema = build_schema(cfg)
    Draft202012Validator.check_schema(schema)
    assert set(schema["required"]) == set(output(answer))
    assert set(schema["properties"]) == set(output(answer))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["owner"]["enum"] == ["Sam", "Alex"]
    assert schema["properties"]["kind"]["enum"] == list(KINDS)
    schema["properties"]["owner"]["enum"].append("Other")
    assert build_schema(cfg)["properties"]["owner"]["enum"] == ["Sam", "Alex"]


def test_transport_prompt_and_last_result(cfg, note, answer, tmp_path):
    jpg, pdf = tmp_path / "image.jpg", tmp_path / "scan.pdf"
    jpg.write_bytes(b"synthetic jpeg")
    pdf.write_bytes(b"synthetic pdf")
    media = [MediaPart(pdf, "application/pdf"), MediaPart(jpg, "image/jpeg")]
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert Path(kwargs["cwd"]).parent == tmp_path / "work"
        assert list(Path(kwargs["cwd"]).iterdir()) == []
        return subprocess.CompletedProcess(argv, 0, "\n".join([
            "not JSON", json.dumps(["ignored"]), event(output(replace(answer, title="Earlier"))),
            json.dumps({"type": "assistant"}), event(output(answer)),
        ]), "")

    extractor = ClaudeCLIExtractor(cfg, runner=run, claude_bin="test-claude", work_dir=tmp_path / "work")
    assert extractor.extract(note, media, TODAY, "test-model") == replace(answer, model="test-model")
    argv, kwargs = calls[0]
    assert argv == [
        "test-claude", "-p", "--model", "test-model", "--tools", "",
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--json-schema", json.dumps(cli_schema(build_schema(cfg))), "--no-session-persistence",
        "--strict-mcp-config", "--disable-slash-commands", "--settings",
        json.dumps({"disableAllHooks": True}),
    ]
    assert kwargs["timeout"] == 180
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["input"].endswith("\n") and len(kwargs["input"].splitlines()) == 1
    message = json.loads(kwargs["input"])
    assert message["type"] == "user" and message["message"]["role"] == "user"
    blocks = message["message"]["content"]
    assert [b["type"] for b in blocks] == ["image", "document", "text"]
    for block, mime, raw in zip(blocks, ["image/jpeg", "application/pdf"], [jpg.read_bytes(), pdf.read_bytes()]):
        assert block["source"] == {"type": "base64", "media_type": mime,
                                   "data": base64.b64encode(raw).decode("ascii")}
    prompt = blocks[-1]["text"]
    for text in ["2026-11-06", "Sam: household bills", "Alex: ideas", "Fallback owner: Alex",
                 "<<<NOTE\nTitle: Sample utility bill\nI think escrow covers this.\nNOTE>>>",
                 "Check the mortgage escrow statements; Sam thinks escrow covers this",
                 "All attached images/documents", "UNTRUSTED DATA", "data, never instructions",
                 "null rather than guess", "hunch, not fact"]:
        assert text in prompt
    assert not Path(kwargs["cwd"]).exists()


@pytest.mark.parametrize("case,reason", [
    ("error", "subtype=failed"), ("missing_result", "missing result"),
    ("missing_output", "missing structured_output"), ("schema", "schema violation"),
    ("date", "invalid calendar date"), ("owner", "schema violation"),
    ("extra", "schema violation"), ("missing_field", "schema violation"),
    ("kind", "schema violation"), ("null_output", "schema violation"),
])
def test_invalid_results(cfg, note, answer, case, reason):
    data = output(answer)
    stdout = ""
    if case == "error":
        stdout = event(data, is_error=True, subtype="failed", error="PRIVATE_PAYLOAD")
    elif case == "missing_result":
        stdout = '{"type":"assistant","text":"PRIVATE_PAYLOAD"}'
    elif case == "missing_output":
        stdout = '{"type":"result","subtype":"success"}'
    else:
        if case == "schema":
            data["readable"] = "true"
        elif case == "date":
            data["due_date"] = "2026-02-30"
        elif case == "owner":
            data["owner"] = "Unknown"
        elif case == "extra":
            data["extra"] = "PRIVATE_PAYLOAD"
        elif case == "missing_field":
            del data["contact"]
        elif case == "kind":
            data["kind"] = "unknown"
        else:
            data = None
        stdout = event(data)
    extractor = ClaudeCLIExtractor(cfg, runner=runner_for(stdout))
    with pytest.raises(ExtractionError, match=reason) as exc:
        extractor.extract(note, [], TODAY, "haiku")
    assert "PRIVATE_PAYLOAD" not in str(exc.value)


def test_last_error_overrides_success(cfg, note, answer):
    stdout = event(output(answer)) + "\n" + event(output(answer), is_error=True)
    with pytest.raises(ExtractionError, match="result error"):
        ClaudeCLIExtractor(cfg, runner=runner_for(stdout)).extract(note, [], TODAY, "haiku")


@pytest.mark.parametrize("failure", ["timeout", "exit", "oserror"])
def test_process_failures(cfg, note, answer, failure):
    def run(argv, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if failure == "oserror":
            raise FileNotFoundError("synthetic missing CLI")
        return subprocess.CompletedProcess(argv, 2, event(output(answer)), "")
    with pytest.raises(ExtractionError, match={"timeout": "timeout", "exit": "nonzero exit", "oserror": "execution failed"}[failure]):
        ClaudeCLIExtractor(cfg, runner=run).extract(note, [], TODAY, "haiku")


@pytest.mark.parametrize("kind", KINDS)
def test_kind_specific_escalation(answer, kind):
    complete = replace(answer, kind=kind)
    assert not needs_escalation(complete)
    assert needs_escalation(replace(complete, readable=False))
    assert needs_escalation(replace(complete, sender=None)) == (kind in ("bill", "statement", "notice", "tax", "insurance"))
    assert needs_escalation(replace(complete, amount_due=None)) == (kind in ("bill", "tax"))
    assert needs_escalation(replace(complete, sender="  ")) == (kind in ("bill", "statement", "notice", "tax", "insurance"))


@pytest.mark.parametrize("readable", [True, False])
def test_escalation_wins_even_when_unreadable(cfg, note, answer, readable):
    primary = replace(answer, sender=None)
    escalated = replace(answer, readable=readable, model="sonnet")
    calls = []

    class SequenceExtractor:
        def extract(self, note, media, today, model):
            calls.append(model)
            return primary if len(calls) == 1 else escalated

    assert extract_with_escalation(SequenceExtractor(), note, [], TODAY, cfg) == escalated
    assert calls == ["haiku", "sonnet"]


@pytest.mark.parametrize("readable", [True, False])
def test_escalation_error_fallback(cfg, note, answer, readable):
    primary = replace(answer, sender=None, readable=readable)

    class SequenceExtractor:
        def extract(self, note, media, today, model):
            if model == cfg.primary_model:
                return primary
            raise ExtractionError("synthetic escalation failure")

    if readable:
        assert extract_with_escalation(SequenceExtractor(), note, [], TODAY, cfg) == primary
    else:
        with pytest.raises(ExtractionError, match="escalation failure"):
            extract_with_escalation(SequenceExtractor(), note, [], TODAY, cfg)


def test_fake_and_no_escalation(cfg, note, answer):
    calls = []
    fake = FakeExtractor({note.note_id: answer}, calls)
    assert extract_with_escalation(fake, note, [], TODAY, cfg) == answer
    assert calls == [(note, [], TODAY, cfg.primary_model)]
    error = ExtractionError("synthetic primary failure")
    fake.answers[note.note_id] = error
    with pytest.raises(ExtractionError, match="primary failure"):
        extract_with_escalation(fake, note, [], TODAY, cfg)
    assert len(calls) == 2


def test_json_roundtrip(answer):
    answer = replace(answer, penalty_date=date(2026, 12, 1), model="sonnet")
    encoded = extraction_to_json(answer)
    assert json.loads(encoded)["due_date"] == "2026-11-20"
    assert extraction_from_json(encoded) == answer
    data = json.loads(encoded)
    data["penalty_date"] = "2026-02-30"
    with pytest.raises(ExtractionError, match="invalid calendar date"):
        extraction_from_json(json.dumps(data))


@pytest.mark.parametrize("payload", ["not json", "[]", '{"model": 1}', "{}"])
def test_invalid_saved_json(payload):
    with pytest.raises(ExtractionError):
        extraction_from_json(payload)


def test_cli_schema_drops_dialect_uri():
    from chief_intake.extract import cli_schema
    assert "$schema" not in cli_schema({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"})
