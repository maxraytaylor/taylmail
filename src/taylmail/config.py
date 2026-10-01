"""Settings, all from environment variables so aerc and git can call us bare."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    home: Path  # state: session map, spool, logs, locks
    maildir: Path  # the Maildir folder aerc reads (has cur/new/tmp)
    domain: str  # claude+SESSION@domain
    cwd: Path  # where the agent works (usually a git repo)
    claude_cli: str
    permission_mode: str
    model: str | None

    def agent_address(self, session_id: str | None = None) -> str:
        local = f"claude+{session_id}" if session_id else "claude"
        return f"{local}@{self.domain}"

    def agent_from(self, session_id: str | None = None) -> str:
        return f"Claude <{self.agent_address(session_id)}>"


def load() -> Config:
    env = os.environ.get
    data = Path(env("XDG_DATA_HOME", Path.home() / ".local/share"))
    return Config(
        home=Path(env("TAYLMAIL_HOME", data / "taylmail")).expanduser(),
        maildir=Path(env("TAYLMAIL_MAILDIR", "~/Maildir/taylmail/INBOX")).expanduser(),
        domain=env("TAYLMAIL_DOMAIN", "taylmail.localhost"),
        cwd=Path(env("TAYLMAIL_CWD", os.getcwd())).expanduser(),
        # The pip SDK's bundled binary can't run on NixOS; prefer the system one.
        claude_cli=env("TAYLMAIL_CLAUDE")
        or shutil.which("claude")
        or "/run/current-system/sw/bin/claude",
        permission_mode=env("TAYLMAIL_PERMISSION_MODE", "acceptEdits"),
        model=env("TAYLMAIL_MODEL"),
    )
