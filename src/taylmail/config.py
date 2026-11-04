"""Settings, all from environment variables so aerc, git and systemd can call us bare."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Server:
    """A mail server from a URL like imaps://host:993 or smtp://host:587."""

    host: str
    port: int
    tls: str  # "implicit", "starttls" or "none"

    @classmethod
    def parse(cls, url: str, default_ports: dict[str, int]) -> "Server":
        parts = urlsplit(url)
        scheme = parts.scheme
        tls = (
            "implicit" if scheme.endswith("s")
            else "none" if scheme.endswith("+insecure")
            else "starttls"
        )
        base = scheme.removesuffix("+insecure")
        if base not in default_ports or not parts.hostname:
            raise ValueError(f"bad mail server URL: {url!r}")
        return cls(parts.hostname, parts.port or default_ports[base], tls)


@dataclass(frozen=True)
class Config:
    home: Path  # state: session map, inbox, outbox, logs
    maildir: Path  # local mode: the Maildir folder aerc reads (has cur/new/tmp)
    address: str  # the agent's address; sessions use local+SESSION@domain
    users: tuple[str, ...]  # who may talk to the agent by email; the first gets its mail
    cwd: Path  # where new sessions work (usually a git repo)
    claude_cli: str
    permission_mode: str
    model: str | None
    # Remote mail; unset means local mode (u2a spool in, Maildir out).
    imap: Server | None = None
    smtp: Server | None = None
    mail_user: str | None = None
    mail_password_file: Path | None = None
    poll_seconds: float = 15.0
    trust_from: bool = False  # skip the Authentication-Results check (dev servers)
    # Open WebUI Channel App; unset key file means no chat.
    chat_listen: str = "127.0.0.1:8000"
    chat_api_key_file: Path | None = None
    chat_private_key_file: Path | None = None
    chat_users: tuple[str, ...] = ()  # Open WebUI user IDs; empty allows channel members

    @property
    def local(self) -> str:
        return self.address.split("@", 1)[0]

    @property
    def domain(self) -> str:
        return self.address.split("@", 1)[1]

    def agent_address(self, session_id: str | None = None) -> str:
        local = f"{self.local}+{session_id}" if session_id else self.local
        return f"{local}@{self.domain}"

    def agent_from(self, session_id: str | None = None) -> str:
        return f"Claude <{self.agent_address(session_id)}>"

    @property
    def user(self) -> str | None:
        return self.users[0] if self.users else None


def _list(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


def _path(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


def load() -> Config:
    env = os.environ.get
    data = Path(env("XDG_DATA_HOME", Path.home() / ".local/share"))
    imap, smtp = env("TAYLMAIL_IMAP"), env("TAYLMAIL_SMTP")
    return Config(
        home=Path(env("TAYLMAIL_HOME", data / "taylmail")).expanduser(),
        maildir=Path(env("TAYLMAIL_MAILDIR", "~/Maildir/taylmail/INBOX")).expanduser(),
        address=env("TAYLMAIL_ADDRESS", "claude@taylmail.localhost"),
        users=_list(env("TAYLMAIL_USERS")),
        cwd=Path(env("TAYLMAIL_CWD", os.getcwd())).expanduser(),
        # The pip SDK's bundled binary can't run on NixOS; prefer the system one.
        claude_cli=env("TAYLMAIL_CLAUDE")
        or shutil.which("claude")
        or "/run/current-system/sw/bin/claude",
        permission_mode=env("TAYLMAIL_PERMISSION_MODE", "auto"),
        model=env("TAYLMAIL_MODEL"),
        imap=Server.parse(imap, {"imap": 143, "imaps": 993}) if imap else None,
        smtp=Server.parse(smtp, {"smtp": 587, "smtps": 465}) if smtp else None,
        mail_user=env("TAYLMAIL_MAIL_USER"),
        mail_password_file=_path(env("TAYLMAIL_MAIL_PASSWORD_FILE")),
        poll_seconds=float(env("TAYLMAIL_POLL_SECONDS", "15")),
        trust_from=env("TAYLMAIL_TRUST_FROM") == "1",
        chat_listen=env("TAYLMAIL_CHAT_LISTEN", "127.0.0.1:8000"),
        chat_api_key_file=_path(env("TAYLMAIL_CHAT_API_KEY_FILE")),
        chat_private_key_file=_path(env("TAYLMAIL_CHAT_PRIVATE_KEY_FILE")),
        chat_users=_list(env("TAYLMAIL_CHAT_USERS")),
    )
