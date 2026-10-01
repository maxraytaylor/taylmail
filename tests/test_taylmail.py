"""Run with: .venv/bin/python -m unittest discover tests"""

import email
import email.policy
import mailbox
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from taylmail import config
from taylmail.a2u import deliver
from taylmail.agent import reply_to
from taylmail.store import Store
from taylmail.u2a import spool

SID = "0d1fff3b-bba5-4aac-9877-7841079207bb"


def parse(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.default)


def patch_mail(n: int) -> bytes:
    return f"""\
From: Claude <claude+{SID}@taylmail.localhost>
To: you@example.com
Subject: [PATCH {n}/2] something
Message-ID: <patch-{n}@taylmail.localhost>

diff
""".encode()


def user_mail(to: str, refs: str = "") -> bytes:
    head = f"In-Reply-To: {refs.split()[-1]}\nReferences: {refs}\n" if refs else ""
    return f"""\
From: You <you@example.com>
To: {to}
Subject: Re: [PATCH 2/2] something
Message-ID: <reply-1@example.com>
{head}
Looks good.
""".encode()


class TaylmailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = replace(
            config.load(), home=self.tmp / "state", maildir=self.tmp / "Maildir/INBOX", cwd=self.tmp
        )

    def test_a2u_delivers_and_records(self):
        deliver(self.cfg, parse(patch_mail(1)), session_id=None)
        box = mailbox.Maildir(self.cfg.maildir)
        self.assertEqual(len(box), 1)
        self.assertEqual(Store(self.cfg.home).lookup(["<patch-1@taylmail.localhost>"]), SID)
        self.assertEqual(Store(self.cfg.home).session(SID)["sent"], 1)

    def test_a2u_rewrites_plain_from(self):
        msg = parse(patch_mail(1).replace(f"claude+{SID}".encode(), b"claude"))
        deliver(self.cfg, msg, session_id=SID)
        (stored,) = mailbox.Maildir(self.cfg.maildir)
        self.assertIn(f"claude+{SID}@", stored["From"])

    def test_u2a_finds_session_by_plus_address(self):
        sid, path = spool(self.cfg, user_mail(f"claude+{SID}@taylmail.localhost"), [])
        self.assertEqual(sid, SID)
        self.assertTrue(Path(path).name.endswith(f"-{SID}.eml"))

    def test_u2a_falls_back_to_references(self):
        deliver(self.cfg, parse(patch_mail(2)), session_id=None)
        refs = "<patch-0@taylmail.localhost> <patch-2@taylmail.localhost>"
        sid, _ = spool(self.cfg, user_mail("someone-else@example.com", refs), [])
        self.assertEqual(sid, SID)

    def test_u2a_new_conversation(self):
        sid, _ = spool(self.cfg, user_mail("claude@taylmail.localhost"), [])
        self.assertNotEqual(sid, SID)
        info = Store(self.cfg.home).session(sid)
        self.assertEqual(info["user"], "you@example.com")
        self.assertFalse(info["started"])
        # The user's own mail is kept, marked seen, for threading in aerc.
        (kept,) = mailbox.Maildir(self.cfg.maildir)
        self.assertIn("S", kept.get_flags())

    def test_reply_threads(self):
        original = parse(user_mail(f"claude+{SID}@x", "<a@x> <b@x>"))
        reply = reply_to(self.cfg, original, SID, "ok")
        self.assertEqual(reply["Subject"], "Re: [PATCH 2/2] something")
        self.assertEqual(reply["In-Reply-To"], "<reply-1@example.com>")
        self.assertEqual(reply["References"], "<a@x> <b@x> <reply-1@example.com>")
        self.assertIn(f"claude+{SID}@", reply["From"])


if __name__ == "__main__":
    unittest.main()
