# chief-intake

Turn one Apple Notes folder into dated Markdown records and proposed hand-offs on a vault board.

```text
Apple Notes folder → prepare images/PDFs → Claude Haiku (Sonnet when needed)
                   → dates and urgency → Markdown record + original images
                   → owner board line → daily run report → explicit-path git commit
```

State checkpoints let interrupted runs resume. Changed notes create new versions; human edits to a record are preserved in place, with a linked `(update N)` record for the new version. Unreadable or unsupported notes stay in Notes for attention. Intake proposes a first step; it does not pay bills, contact senders, or execute instructions in captured content.

## Requirements and install

The live path requires macOS, Python 3.11+, Apple Notes, and the Claude Code CLI installed and logged in. The vault must be a local Markdown directory; git must already have an initial commit when automatic commits are enabled. Unit tests work without Notes, Claude, or macOS tools.

```sh
git clone <your-chief-intake-repository-url>
cd chief-intake
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
mkdir -p ~/.config/chief-intake
cp config.example.toml ~/.config/chief-intake/config.toml
```

Edit [config.example.toml](config.example.toml)'s copied settings: Notes folder (and account if ambiguous), vault path, existing inbox directory, board path, owners, fallback owner and state directory. All user-specific routing belongs in that file. Create a Markdown board whose final second-level heading is `## Open`, or set `board_section` to its final heading. Keep state outside the vault and cloud sync folders.

Activate the virtual environment or use `.venv/bin/chief-intake`. Configuration defaults to `~/.config/chief-intake/config.toml`; `CHIEF_INTAKE_CONFIG` or `--config PATH` overrides it.

## Commands

```sh
chief-intake run --dry-run --out /tmp/intake-preview
chief-intake run --limit 5 --config ~/.config/chief-intake/config.toml
chief-intake run
chief-intake status
chief-intake status --json
chief-intake cleanup --dry-run
chief-intake cleanup
chief-intake doctor
chief-intake schedule print
chief-intake schedule install
chief-intake schedule test
chief-intake schedule remove
```

`run` processes new and changed content, resumes unfinished stages, retries held commits and performs cleanup. Notes modified within ten minutes wait for a later run. Missing or stale attachments wait for sync; after 24 hours they appear in the report. That clock follows the unresolved note across incremental attachment sync and resets after successful processing. Returning to earlier content creates a fresh processed occurrence and restarts the deletion clock. Unsupported attachments, oversized input and unreadable content need attention. Extraction failures retry on later runs, up to three failures per fingerprint.

Dry runs write records, original media and a report only under `--out` (a temporary directory if omitted). They do not update the board, persistent state, git or Notes. Review the printed output directory before a first real run. `cleanup --dry-run` lists proposed deletions and expiry without applying them.

Reports group overdue, due-soon, routed and updated items, processing problems, waiting items, commit problems and cleanup. Empty runs create no report. `last-run.json` and `runs.log` in the state directory record real-run outcomes even when the vault cannot be written. `status --json` returns `notes` (each with `note_id`, `title`, `state`, `last_error`) and `last_run`. A concurrent run exits successfully with `already running`.

Notes become eligible for deletion 14 days after their latest version completes its record, board and report stages. Cleanup checks record identity, image hashes, and fresh Notes folder/modification state, then verifies the note left the intake folder after deletion. Any uncertainty skips deletion and is reported. Notes moves deleted items to Recently Deleted. Original media copies and any viewing derivatives expire together per version after 365 days; records remain and receive an expiry marker. Expiry record changes and cleanup reports remain pending in state until their git commit succeeds, including after a crash.

## Scheduling and macOS permissions

`schedule install` writes and loads a user LaunchAgent for 07:00 daily. It invokes the virtual environment's Python directly with `run --trigger launchd --config <absolute path>`, preserving the configuration selected at install time. Sleep delays execution until wake; a powered-off Mac catches up on its next scheduled or manual run. Logs go to `state_dir/launchd.log`.

1. Run `schedule install` and grant **Full Disk Access** in System Settings → Privacy & Security to the resolved Python binary printed by the installer. This allows the scheduled process to read Notes and write a vault under Documents.
2. While at the Mac, run `schedule test`. It loads and kickstarts a separate one-shot `<label>.preflight` job whose arguments include `run --preflight --config <absolute path>`: read the configured folder, write and remove an inbox probe, and ask Notes for its folder count. Approve the **Automation / control Notes** prompt. No extraction or deletion occurs. Preflight runs only with the explicit `--preflight` flag; ordinary terminal runs cannot consume a preflight request.
3. Read `status` and run `doctor`. Doctor prints one `PASS` or `FAIL` line per check with a fix hint. Exit code is zero only when all checks pass. A terminal check alone cannot prove the scheduled process has permission; inspect the recorded preflight result. `last-run.json` records `trigger` as `terminal`, `launchd`, or `preflight`. `last-scheduled-run.json` retains the latest launchd/preflight outcome; doctor reads that file, so terminal success cannot hide a scheduled failure.

After a Python upgrade, repeat the grants and schedule test for the new resolved binary. The installer resolves `--config`, `CHIEF_INTAKE_CONFIG`, or the default path into an absolute argument in the plist. Reinstall the job if its Python or configuration path changes. Use the same selected configuration when running `schedule test`.

## Privacy and git

Only notes from the configured folder are decoded; Recently Deleted is excluded. Claude receives the note title/text, all prepared images or PDF pages, today's date, and the configured owner descriptions and user name. Processing is subject to your Claude account's data handling policies. Treat the content you capture accordingly.

Chief Intake keeps its persistent files in your vault and configured state directory. Dry-run output goes to the requested scratch directory. Media preparation uses a per-run work directory and removes it after the run. Extraction runs in an empty working directory, with tools, MCP servers, hooks, slash commands and session persistence disabled. Claude's user-level instructions may still load, and external tools can maintain their own account metadata.

Original media stays beside the records with its exact bytes, while resized copies are sent to the model. For HEIC/HEIF, intake retains `<record base> N.heic` (or `.heif`) and also creates `<record base> N.jpg` for viewing. The record embeds the JPEG; both files are hashed in that version’s state, verified before deleting the source note, ignored by git, and retired together. Other formats retain only the original vault copy. Intake installs extension-specific `.gitignore` rules and verifies images are ignored. Commits use explicit Markdown and `.gitignore` paths with `git commit --only`; intake never stages binaries or unrelated staged files. If the board has someone else's uncommitted edits, intake writes its hand-off but excludes the board from its commit and reports why. Failed commits leave files available for review and retry. State stores SHA-256 hashes of intake output, including reports after each append. A retry commits a path only while its current hash matches what intake last wrote; otherwise the report says `not committed: edited since intake wrote it`. Shared reports already edited by a person remain excluded after an intake append. `.gitignore` is included only when this run changed it and it was clean against HEAD beforehand. Board updates replace only the exact previously stored intake line; human annotations are preserved and receive a separate `UPDATED:` hand-off.

Records, reports and local state can contain sensitive extracted information. Git history retains committed text after later edits; review your vault's sharing and backup policies. Model-supplied fields are sanitized before Markdown rendering.

## Testing

All checked-in examples are synthetic. Tests use fixtures, fake model responses, fake Notes controls and temporary git repositories.

```sh
.venv/bin/pytest -q
CHIEF_INTAKE_LIVE=1 .venv/bin/pytest -q -s -m live
```

The live test calls the real logged-in Claude CLI on every synthetic fixture, including a two-page PDF, scores against the answer key and requires at least 90% of scored fields. It is skipped by default and needs network access. No test needs real Notes or a real vault.

## Limits

This is a single-machine intake tool, not a task executor. It cannot prove a phone edit has synced; the age margin and fresh modification check reduce that risk. It cannot know whether a paper scan includes every page, so PDF page counts are shown for review. Lightweight PDF validation does not support every PDF encoding. A concurrent whole-board rewrite by another application can race intake's append. Reports may repeat an item after a crash between writing and checkpointing. The weekly planning backstop and an API-key extraction backend are not implemented.
