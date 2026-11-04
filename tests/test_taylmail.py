"""Run with: .venv/bin/python -m unittest discover tests"""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from taylmail import chat, config, inbox, mail
from taylmail.store import Store

SID = "0d1fff3b-bba5-4aac-9877-7841079207bb"


def patch_mail(n: int, frm: str = f"claude+{SID}") -> bytes:
    return f"""\
From: Claude <{frm}@taylmail.localhost>
To: you@example.com
Subject: [PATCH {n}/2] something
Message-ID: <patch-{n}@taylmail.localhost>

diff
""".encode()


def user_mail(to: str, refs: str = "", frm: str = "you@example.com", extra: str = "") -> bytes:
    head = f"In-Reply-To: {refs.split()[-1]}\nReferences: {refs}\n" if refs else ""
    return f"""\
{extra}Return-Path: <{frm}>
From: You <{frm}>
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
            config.load(),
            home=self.tmp / "state",
            cwd=self.tmp,
            address="claude@taylmail.localhost",
            users=("you@example.com",),
            sender_check="return-path",
        )
        self.sent = []
        smtp = mock.patch.object(mail, "_smtp_send", lambda cfg, msg, rcpts: self.sent.append(msg))
        smtp.start()
        self.addCleanup(smtp.stop)

    def queued(self) -> list[inbox.Job]:
        return [j for jobs in inbox.pending(self.cfg).values() for j in jobs]

    def test_send_delivers_and_records(self):
        mail.send(self.cfg, mail.parse(patch_mail(1)), session_id=None)
        self.assertEqual(len(self.sent), 1)
        store = Store(self.cfg.home)
        self.assertEqual(store.lookup(["<patch-1@taylmail.localhost>"]), SID)
        self.assertEqual(store.session(SID)["sent"], 1)

    def test_send_rewrites_plain_from(self):
        mail.send(self.cfg, mail.parse(patch_mail(1, frm="claude")), session_id=SID)
        (stored,) = self.sent
        self.assertIn(f"claude+{SID}@", stored["From"])
        self.assertIn(f"claude+{SID}@", stored["Reply-To"])

    def test_outbox_round_trip(self):
        mail.queue_outgoing(self.cfg, patch_mail(1), ["you@example.com"], SID)
        ((sid, msg),) = mail.drain_outbox(self.cfg)
        self.assertEqual((sid, msg["Subject"]), (SID, "[PATCH 1/2] something"))
        self.assertEqual(mail.drain_outbox(self.cfg), [])

    def test_envelope_only_mails_users(self):
        msg = mail.parse(patch_mail(1))
        rcpts = mail._envelope(self.cfg, msg, ["evil@example.org", "You@Example.com"])
        self.assertEqual(rcpts, ["you@example.com"])

    def test_intake_finds_session_by_plus_address(self):
        Store(self.cfg.home).update_session(SID, started=False)
        sid = mail.intake(self.cfg, user_mail(f"claude+{SID}@taylmail.localhost"))
        self.assertEqual(sid, SID)
        (job,) = self.queued()
        self.assertEqual((job.session, job.source), (SID, "email"))
        self.assertEqual(job.email["message_id"], "<reply-1@example.com>")

    def test_intake_falls_back_to_references(self):
        mail.send(self.cfg, mail.parse(patch_mail(2)), session_id=None)
        refs = "<patch-0@taylmail.localhost> <patch-2@taylmail.localhost>"
        sid = mail.intake(self.cfg, user_mail("someone-else@example.com", refs))
        self.assertEqual(sid, SID)

    def test_intake_new_conversation(self):
        sid = mail.intake(self.cfg, user_mail("claude@taylmail.localhost"))
        self.assertNotIn(sid, (None, SID))

    def test_intake_ignores_unknown_session(self):
        sid = mail.intake(self.cfg, user_mail(f"claude+{SID}@taylmail.localhost"))
        self.assertNotIn(sid, (None, SID))

    def test_intake_drops_strangers(self):
        raw = user_mail("claude@taylmail.localhost", frm="evil@example.org")
        self.assertIsNone(mail.intake(self.cfg, raw))
        self.assertEqual(self.queued(), [])

    def test_intake_needs_authentication_unless_trusted(self):
        self.cfg = replace(self.cfg, sender_check="dmarc")
        raw = user_mail("claude@taylmail.localhost")
        self.assertIsNone(mail.intake(self.cfg, raw))
        good = "Authentication-Results: mx.example.net; dmarc=pass (p=none) header.from=example.com\n"
        self.assertIsNotNone(mail.intake(self.cfg, user_mail("claude@taylmail.localhost", extra=good)))

    def test_return_path_must_match_from(self):
        forged = "Return-Path: <evil@example.org>\n"
        raw = user_mail("claude@taylmail.localhost", extra=forged)
        self.assertIsNone(mail.intake(self.cfg, raw))

    def test_only_topmost_authentication_results_counts(self):
        top = "Authentication-Results: mx.example.net; dmarc=fail header.from=example.com\n"
        forged = "Authentication-Results: mx.example.net; dmarc=pass header.from=example.com\n"
        msg = mail.parse(user_mail("claude@taylmail.localhost", extra=top + forged))
        self.assertFalse(mail.authenticated(msg))

    def test_dkim_pass_for_sender_domain(self):
        ar = "Authentication-Results: mx.example.net; dkim=pass header.d=example.com header.s=s1\n"
        self.assertTrue(mail.authenticated(mail.parse(user_mail("x@y.z", extra=ar))))
        ar = "Authentication-Results: mx.example.net; dkim=pass header.d=evil.org\n"
        self.assertFalse(mail.authenticated(mail.parse(user_mail("x@y.z", extra=ar))))

    def test_chat_thread_links_to_session(self):
        store = Store(self.cfg.home)
        target = {"installation_id": "i", "channel_id": "c", "thread_id": "m"}
        store.link_thread("c/m", SID, target)
        self.assertEqual(store.thread_session("c/m"), SID)
        self.assertEqual(store.session(SID)["chat"], target)

    def test_chat_catches_up_on_missed_messages(self):
        def msg(id, t, parent=None):
            author = {"type": "user", "id": "u1"}
            return {"id": id, "channel_id": "c", "author": author, "content": id,
                    "parent_id": parent, "created_at": str(t * 10**12)}

        class Client:
            installation_id = "i"

            async def list_channels(self):
                return [{"id": "c"}]

            async def list_messages(self, channel_id, limit, before, parent_id):
                # Newest first, like Open WebUI.
                rows = [m for m in history if m["parent_id"] == parent_id]
                return {"messages": rows[::-1], "next_cursor": None}

        wake = mock.Mock()
        history = [msg("old", 1), msg("root", 2)]
        chat.handle(self.cfg, wake, "i", history[1])
        history += [msg("reply", 3, parent="root"), msg("new", 4)]
        asyncio.run(chat.catch_up(self.cfg, wake, Client()))
        asyncio.run(chat.catch_up(self.cfg, wake, Client()))
        texts = sorted(j.prompt.rsplit("\n", 1)[-1] for j in self.queued())
        self.assertEqual(texts, ["new", "reply", "root"])

    def test_server_urls(self):
        p = {"imap": 143, "imaps": 993}
        self.assertEqual(config.Server.parse("imaps://mail.example.com", p).port, 993)
        self.assertEqual(config.Server.parse("imap://h:1143", p).tls, "starttls")
        self.assertEqual(config.Server.parse("imap+insecure://h", p).tls, "none")


if __name__ == "__main__":
    unittest.main()
