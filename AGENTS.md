# Repo instructions for coding agents

chief-intake turns an Apple Notes folder into routed, dated Markdown records in a vault.
The design (source of truth for behavior) is passed to you by path in your task prompt.

## Rules
- **Public repo. Synthetic data only.** Never put real names, addresses, account numbers,
  phone numbers, note text or images from a real person into code, tests, fixtures or docs.
  Use 555-01xx phone numbers, example.com, "Sam", "Alex", "J. Sample".
- Never read Apple Notes data, `~/Library/Group Containers`, or any vault outside a temp dir.
- Python >= 3.11, standard library plus `apple-notes-parser` and `jsonschema` (already in `.venv`).
  No network in the sandbox: don't pip install anything.
- `chief_intake/models.py` and `chief_intake/config.py` are the shared contract. Don't change
  them unless your task says you own them; if you need a change, say so in your final message.
- Stay inside the files your task lists. Other agents are writing other files in parallel.
- Tests: `.venv/bin/pytest -q`. Every module gets unit tests in `tests/test_<module>.py`.
  Tests must not need macOS tools, Notes, network or the `claude` CLI; inject runners/fakes.
  Live tests are marked `@pytest.mark.live` and skip unless `CHIEF_INTAKE_LIVE=1`.
- External commands (`claude`, `osascript`, `sips`, `git`, `launchctl`) go through a small
  injectable runner (a callable taking argv list, returning CompletedProcess) so tests can fake them.
- Readable code, explicit errors, type hints, no frameworks. Keep modules small.
- You can't `git commit` in the sandbox; leave changes in the working tree. The host commits.

## Final message
What you built (files), tests run and their result (paste the pytest summary line),
anything you couldn't do, and any contract change you need.
