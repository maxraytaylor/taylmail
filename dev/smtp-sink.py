"""Dev SMTP server: delivers each recipient's mail into Dovecot's Maildir.

Usage: python dev/smtp-sink.py MAILROOT PORT
Plus-addresses (claude+x@...) go to the base user, like most providers.
"""

import asyncio
import mailbox
import sys
from pathlib import Path

from aiosmtpd.controller import Controller

root = Path(sys.argv[1])


class Deliver:
    async def handle_DATA(self, server, session, envelope):
        for rcpt in envelope.rcpt_tos:
            local, _, domain = rcpt.lower().partition("@")
            user = f"{local.split('+')[0]}@{domain}"
            box = mailbox.Maildir(root / user, create=True)
            box.add(envelope.original_content)
            print(f"smtp-sink: {envelope.mail_from} -> {rcpt}", flush=True)
        return "250 OK"


async def main():
    Controller(Deliver(), hostname="127.0.0.1", port=int(sys.argv[2])).start()
    await asyncio.Event().wait()


asyncio.run(main())
