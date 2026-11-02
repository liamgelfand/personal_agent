"""Loads config.yaml: the repos the agent may touch and per-role model choices."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
SCRATCH_DIR = DATA_DIR / "scratch"  # working directory for conversations with no repo
WORKTREES_DIR = DATA_DIR / "worktrees"  # one git worktree per agent task


class Access(str, Enum):
    READ = "read"  # agents may read and run commands, never edit files
    WRITE = "write"  # agents may edit files in the repo


class Repo(BaseModel):
    name: str
    path: Path
    access: Access = Access.READ
    github: str | None = None  # "owner/repo", used by the GitHub integration later

    @field_validator("path")
    @classmethod
    def _resolve(cls, value: Path) -> Path:
        path = value.expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"repo path does not exist: {path}")
        return path


class Models(BaseModel):
    # Aliases resolve to the newest model your plan can use; full IDs also work.
    orchestrator: str = "sonnet"
    implementer: str = "opus"
    reviewer: str = "opus"
    researcher: str = "sonnet"


class Remote(BaseModel):
    # Off: the server only listens on 127.0.0.1. On: it listens on `host` and every client must
    # log in with the token in data/remote_token. Use a private network such as Tailscale.
    enabled: bool = False
    host: str = "0.0.0.0"


class Notifications(BaseModel):
    # Optional push notifications to your phone through ntfy (https://ntfy.sh). Only short status
    # messages are sent (e.g. "Approval needed"), never code. Pick a long, unguessable topic name.
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"


class Config(BaseModel):
    repos: list[Repo] = Field(default_factory=list)
    models: Models = Field(default_factory=Models)
    max_turns: int = 60
    port: int = 8000
    remote: Remote = Field(default_factory=Remote)
    notifications: Notifications = Field(default_factory=Notifications)
    # "subscription": always use your claude.ai plan login (API keys in the environment are ignored).
    # "api": allow ANTHROPIC_API_KEY to be used, billed to your Claude Platform account.
    # true: commits, pushes, PRs, installs and edits to config/guards run without prompting. Merging a PR
    # always asks, and hard blocks (force push, push to main, reset --hard) still apply.
    auto_approve: bool = False
    billing: Literal["subscription", "api"] = "subscription"

    def repo(self, name: str) -> Repo:
        for repo in self.repos:
            if repo.name == name:
                return repo
        raise KeyError(f"unknown repo: {name}")


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found - copy config.example.yaml to config.yaml and list your repos"
        )
    return Config.model_validate(yaml.safe_load(path.read_text()) or {})
