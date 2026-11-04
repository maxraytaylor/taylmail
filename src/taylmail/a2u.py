"""Agent to user: a sendmail stand-in for `git send-email --sendmail-cmd`.

git appends `-i` and the recipients to the command line. We queue the mail
for `taylmail serve`, which sends it (only ever to TAYLMAIL_USERS) and tells
the session's chat thread.
"""

import os
import sys

from . import mail
from .config import Config


def main(cfg: Config, argv: list[str]) -> int:
    recipients = [a for a in argv if not a.startswith("-")]
    # The agent's environment carries its session ID; see agent.py.
    session_id = os.environ.get("TAYLMAIL_SESSION")
    mail.queue_outgoing(cfg, sys.stdin.buffer.read(), recipients, session_id)
    print("taylmail a2u: queued", file=sys.stderr)
    return 0
