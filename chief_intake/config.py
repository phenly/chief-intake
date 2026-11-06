"""Load config.toml. Everything user-specific lives here, never in code."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("~/.config/chief-intake/config.toml")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Owner:
    name: str
    handles: str


@dataclass(frozen=True)
class Config:
    notes_folder: str
    vault: Path
    inbox_dir: str
    board: str
    owners: tuple[Owner, ...]
    fallback_owner: str
    notes_account: str = ""
    board_section: str = "## Open"
    board_from: str = "Intake"
    user_name: str = "the user"
    act_by_days: int = 7
    min_note_age_minutes: int = 10
    delete_after_days: int = 14
    image_retention_days: int = 365
    git_commit: bool = True
    state_dir: Path = Path("~/.local/state/chief-intake").expanduser()
    primary_model: str = "haiku"
    escalation_model: str = "sonnet"
    notes_container: Path = field(
        default=Path("~/Library/Group Containers/group.com.apple.notes").expanduser()
    )

    @property
    def inbox_path(self) -> Path:
        return self.vault / self.inbox_dir

    @property
    def board_path(self) -> Path:
        return self.vault / self.board

    @property
    def owner_names(self) -> tuple[str, ...]:
        return tuple(o.name for o in self.owners)


def config_path(explicit: str | None = None) -> Path:
    raw = explicit or os.environ.get("CHIEF_INTAKE_CONFIG") or str(DEFAULT_PATH)
    return Path(raw).expanduser()


def load(path: str | Path | None = None) -> Config:
    p = config_path(str(path) if path else None)
    if not p.exists():
        raise ConfigError(f"config not found: {p} (copy config.example.toml there)")
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    return from_dict(raw)


def from_dict(raw: dict) -> Config:
    try:
        owners = tuple(Owner(o["name"], o["handles"]) for o in raw.get("owners", []))
        models = raw.get("models", {})
        cfg = Config(
            notes_folder=raw["notes_folder"],
            vault=Path(raw["vault"]).expanduser(),
            inbox_dir=raw["inbox_dir"],
            board=raw["board"],
            owners=owners,
            fallback_owner=raw["fallback_owner"],
            notes_account=raw.get("notes_account", ""),
            board_section=raw.get("board_section", "## Open"),
            board_from=raw.get("board_from", "Intake"),
            user_name=raw.get("user_name", "the user"),
            act_by_days=int(raw.get("act_by_days", 7)),
            min_note_age_minutes=int(raw.get("min_note_age_minutes", 10)),
            delete_after_days=int(raw.get("delete_after_days", 14)),
            image_retention_days=int(raw.get("image_retention_days", 365)),
            git_commit=bool(raw.get("git_commit", True)),
            state_dir=Path(raw.get("state_dir", "~/.local/state/chief-intake")).expanduser(),
            primary_model=models.get("primary", "haiku"),
            escalation_model=models.get("escalation", "sonnet"),
            notes_container=Path(
                raw.get("notes_container", "~/Library/Group Containers/group.com.apple.notes")
            ).expanduser(),
        )
    except KeyError as e:
        raise ConfigError(f"missing required config key: {e.args[0]}") from None
    if not cfg.owners:
        raise ConfigError("config needs at least one [[owners]] entry")
    if cfg.fallback_owner not in cfg.owner_names:
        raise ConfigError(f"fallback_owner {cfg.fallback_owner!r} must be one of the owners")
    return cfg
