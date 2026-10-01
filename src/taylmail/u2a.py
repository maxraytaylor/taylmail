"""User to agent: aerc's `outgoing` command.

aerc pipes the message on stdin (recipients may also be in argv). We work out
which session it belongs to, spool it, and start the agent in the background
so aerc isn't left waiting for the whole agent turn.
"""

import email
import email.policy
import os
import subprocess
import sys
import time
import uuid
from email.message import EmailMessage
from email.utils import getaddresses

from .a2u import deliver, session_from_addresses
from .config import Config
from .store import Store


def find_session(cfg: Config, msg: EmailMessage, recipients: list[str]) -> str | None:
    """Plus-address first, then the Message-ID map via In-Reply-To/References."""
    to_headers = msg.get_all("To", []) + msg.get_all("Cc", [])
    if sid := session_from_addresses(", ".join(recipients), *to_headers):
        return sid
    refs = (msg["References"] or "").split() + (msg["In-Reply-To"] or "").split()
    return Store(cfg.home).lookup(refs)


def spool(cfg: Config, raw: bytes, recipients: list[str]) -> tuple[str, str]:
    """Save the message and the session it belongs to. Returns (session_id, path)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    store = Store(cfg.home)
    session_id = find_session(cfg, msg, recipients)
    if session_id is None or store.session(session_id) is None:
        session_id = session_id or str(uuid.uuid4())
        _, user = getaddresses([msg["From"] or ""])[0]
        store.update_session(session_id, started=False, user=user, cwd=str(cfg.cwd))

    # Keep the user's half of the conversation in the same folder so aerc
    # can thread it with the agent's mail.
    deliver(cfg, msg, session_id=None, seen=True)
    if mid := msg["Message-ID"]:
        store.record_message(mid.strip(), session_id, sent=False)

    spool_dir = cfg.home / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    path = spool_dir / f"{time.time_ns()}-{session_id}.eml"
    path.write_bytes(raw)
    return session_id, str(path)


def main(cfg: Config, argv: list[str]) -> int:
    foreground = "--foreground" in argv
    recipients = [a for a in argv if not a.startswith("-")]
    session_id, path = spool(cfg, sys.stdin.buffer.read(), recipients)

    cmd = [sys.executable, "-m", "taylmail", "run", path]
    if foreground:
        return subprocess.call(cmd)

    log_dir = cfg.home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / f"{session_id}.log", "ab") as log:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # survive aerc closing
            env=os.environ.copy(),
        )
    print(f"taylmail u2a: queued for session {session_id}", file=sys.stderr)
    return 0
