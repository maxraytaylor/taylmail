"""User to agent, local mode: aerc's `outgoing` command.

aerc pipes the message on stdin (recipients may also be in argv). We queue it
for `taylmail serve` and keep a copy in the Maildir so aerc can thread it with
the agent's mail. With remote mail, aerc sends normally and serve reads IMAP.
"""

import sys

from . import mail
from .config import Config


def main(cfg: Config, argv: list[str]) -> int:
    raw = sys.stdin.buffer.read()
    recipients = [a for a in argv if not a.startswith("-")]
    session_id = mail.intake(cfg, raw, recipients, trusted=True)
    if session_id is None:
        print("taylmail u2a: dropped; see the warning above", file=sys.stderr)
        return 1
    mail.maildir_add(cfg, mail.parse(raw), seen=True)
    print(f"taylmail u2a: queued for session {session_id}", file=sys.stderr)
    return 0
