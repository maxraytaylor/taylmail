"""Agent to user: a sendmail stand-in that drops mail into the user's Maildir.

Used as `git send-email --sendmail-cmd="taylmail a2u"`. git appends `-i` and
the recipients to the command line; we ignore them, since everything goes to
the one Maildir that aerc reads.
"""

import email
import email.policy
import mailbox
import os
import re
import sys
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

from .config import Config
from .store import Store

SESSION_ADDR = re.compile(r"^claude\+([0-9a-f-]+)@", re.IGNORECASE)


def session_from_addresses(*headers: str | None) -> str | None:
    for _, addr in getaddresses([h for h in headers if h]):
        if m := SESSION_ADDR.match(addr):
            return m.group(1).lower()
    return None


def deliver(cfg: Config, msg: EmailMessage, *, session_id: str | None, seen: bool = False) -> str:
    """Put `msg` into the Maildir and remember its Message-ID. Returns the Message-ID."""
    session_id = session_id or session_from_addresses(msg["From"])
    if session_id and not session_from_addresses(msg["From"]):
        # Make sure a plain "Reply" in aerc goes back to this session.
        del msg["From"]
        msg["From"] = cfg.agent_from(session_id)
    if not msg["Message-ID"]:
        msg["Message-ID"] = make_msgid(domain=cfg.domain)
    if not msg["Date"]:
        msg["Date"] = formatdate(localtime=True)

    box = mailbox.Maildir(cfg.maildir, create=True)
    mmsg = mailbox.MaildirMessage(msg.as_bytes())
    if seen:
        mmsg.set_subdir("cur")
        mmsg.add_flag("S")
    box.add(mmsg)

    if session_id:
        Store(cfg.home).record_message(msg["Message-ID"].strip(), session_id, sent=not seen)
    return msg["Message-ID"]


def main(cfg: Config, argv: list[str]) -> int:
    raw = sys.stdin.buffer.read()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    # The agent's environment carries its session ID; see agent.py.
    session_id = os.environ.get("TAYLMAIL_SESSION")
    mid = deliver(cfg, msg, session_id=session_id)
    print(f"taylmail a2u: delivered {mid} to {cfg.maildir}", file=sys.stderr)
    return 0
