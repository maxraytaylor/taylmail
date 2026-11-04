"""taylmail serve: one process that owns every session.

Adapters queue user messages into the inbox; the scheduler runs one turn per
session at a time, batching whatever arrived while the previous turn ran.
Nothing polls: IMAP IDLE, the Open WebUI pipe and a2u (over a Unix socket)
wake us.
"""

import asyncio
import logging
from email.message import EmailMessage

from . import agent, chat, inbox, mail
from .config import Config
from .inbox import Job
from .store import Store

log = logging.getLogger("taylmail")


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.wakeup = asyncio.Event()
        self.outbox = asyncio.Event()
        self.outbox_lock = asyncio.Lock()  # one drain at a time, or mail goes out twice
        self.busy: set[str] = set()
        self.tasks: set[asyncio.Task] = set()  # keep running turns referenced
        self.chat_on = bool(cfg.chat_api_key_file)
        self.waiters: dict[str, asyncio.Queue] = {}  # chat job file -> its /turn stream
        self.live: dict[str, asyncio.Queue] = {}  # session -> stream of its running turn
        self.notes: dict[str, list[str]] = {}  # session -> notices for its turn's event
        self.events_ready = asyncio.Event()

    def wake(self) -> None:
        self.wakeup.set()
        self.outbox.set()

    async def main(self) -> None:
        cfg = self.cfg
        tasks = [self.scheduler(), self.outbox_loop(), self.imap_loop(), self.wake_socket()]
        if self.chat_on:
            tasks.append(self.serve_chat())
        log.info(
            "serving %s: mail %s, chat %s", cfg.address,
            f"IMAP {cfg.imap.host}:{cfg.imap.port}",
            cfg.chat_listen if self.chat_on else "off",
        )
        await asyncio.gather(*tasks)

    # --- adapters ------------------------------------------------------------

    async def imap_loop(self) -> None:
        loop = asyncio.get_running_loop()

        def handle(raw: bytes) -> None:
            if sid := mail.intake(self.cfg, raw):
                log.info("mail for session %s", sid)

        def fetched() -> None:
            loop.call_soon_threadsafe(self.wake)

        while True:
            try:
                await asyncio.to_thread(mail.imap_watch, self.cfg, handle, fetched)
                delay = self.cfg.poll_seconds  # the server can't IDLE
            except Exception as e:
                log.warning("IMAP failed: %s", e)
                delay = 5
            await asyncio.sleep(delay)

    async def wake_socket(self) -> None:
        """a2u connects here after queuing mail."""
        path = inbox.wake_socket(self.cfg)
        path.unlink(missing_ok=True)

        async def ping(reader, writer) -> None:
            self.wake()
            writer.close()

        server = await asyncio.start_unix_server(ping, path=str(path))
        async with server:
            await server.serve_forever()

    async def serve_chat(self) -> None:
        import uvicorn

        from .chat import make_server

        host, _, port = self.cfg.chat_listen.rpartition(":")
        config = uvicorn.Config(
            make_server(self.cfg, self), host=host, port=int(port), log_level="warning"
        )
        await uvicorn.Server(config).serve()

    def event(self, sid: str, reply: str, user: str | None = None, title: str = "") -> None:
        chat.put_event(self.cfg, sid, reply, user, title)
        self.events_ready.set()

    def stream(self, sid: str) -> asyncio.Queue | None:
        """The /turn stream of the session's running turn, if still connected."""
        queue = self.live.get(sid)
        return queue if queue in self.waiters.values() else None

    async def outbox_loop(self) -> None:
        while True:
            self.outbox.clear()
            await self.flush_outbox()
            await self.outbox.wait()

    async def flush_outbox(self) -> None:
        try:
            async with self.outbox_lock:
                sent = await asyncio.to_thread(mail.drain_outbox, self.cfg)
        except Exception as e:
            log.warning("sending mail failed: %s", e)
            return
        by_session: dict[str, list[EmailMessage]] = {}
        for sid, msg in sent:
            log.info("sent %s for session %s", msg["Subject"], sid)
            if sid:
                by_session.setdefault(sid, []).append(msg)
        for sid, msgs in by_session.items():
            if not self.chat_on:
                continue
            lines = "\n".join(f"- {m['Subject']}" for m in msgs)
            note = f"📧 Sent to your inbox:\n{lines}"
            if queue := self.stream(sid):
                queue.put_nowait({"content": note + "\n\n"})
            elif sid in self.busy:
                self.notes.setdefault(sid, []).append(note)
            elif (Store(self.cfg.home).session(sid) or {}).get("chat"):
                self.event(sid, note)

    # --- turns ---------------------------------------------------------------

    async def scheduler(self) -> None:
        while True:
            self.wakeup.clear()
            for sid, jobs in inbox.pending(self.cfg).items():
                if sid not in self.busy:
                    self.busy.add(sid)
                    task = asyncio.create_task(self.turn(sid, jobs))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
            await self.wakeup.wait()

    async def turn(self, sid: str, jobs: list[Job]) -> None:
        cfg, store = self.cfg, Store(self.cfg.home)
        try:
            emails = [j for j in jobs if j.source == "email"]
            chats = [j for j in jobs if j.source == "chat"]
            queue = self.waiters.get(chats[-1].path.name) if chats else None
            if queue:
                self.live[sid] = queue
                for job in emails:
                    queue.put_nowait({"content": f"📧 Also answering your mail: {job.email.get('subject')}\n\n"})

            async def progress(line: str) -> None:
                if q := self.stream(sid):
                    q.put_nowait({"status": line})

            log.info("session %s: turn with %d message(s)", sid, len(jobs))

            sent_before = (store.session(sid) or {}).get("sent", 0)
            error = None
            try:
                result = await agent.run_turn(cfg, sid, jobs, progress=progress)
            except Exception as e:  # tell the user rather than failing silently
                result, error = None, f"{type(e).__name__}: {e}"
            await self.flush_outbox()
            sent = (store.session(sid) or {}).get("sent", 0) > sent_before

            if result is not None and result.is_error:
                error = f"agent stopped: {result.subtype}\n" + "\n".join(result.errors or [])
            body = ""
            if error:
                body = f"[taylmail] This turn failed.\n\n{error}\n"
            elif result is not None:
                body = result.result or ""
                # A denied tool call may be why nothing happened.
                if denials := result.permission_denials:
                    body += "\n\n[taylmail] Permission denied for:\n" + "\n".join(
                        f"- {d.get('tool_name')}: {d.get('tool_input')}" for d in denials
                    )

            if emails and body.strip() and (error or not sent):
                reply = mail.reply_to(cfg, emails[-1].email, sid, body)
                await asyncio.to_thread(mail.send, cfg, reply, session_id=sid)
            if self.chat_on:
                self.reply_in_chat(sid, emails, chats, body.strip() or "Done.")
            else:
                self.notes.pop(sid, None)
            self.live.pop(sid, None)

            for job in jobs:
                job.path.unlink(missing_ok=True)
            if error:
                log.warning("session %s: turn failed: %s", sid, error)
            log.info("session %s: turn done", sid)
        except Exception:
            log.exception("session %s: turn crashed; its messages stay queued", sid)
            await asyncio.sleep(30)  # don't spin on a message that always crashes
        finally:
            self.busy.discard(sid)
            self.wake()

    def reply_in_chat(self, sid: str, emails: list[Job], chats: list[Job], content: str) -> None:
        for job in chats[:-1]:
            if q := self.waiters.get(job.path.name):
                q.put_nowait({"reply": "Answered below."})
        if (queue := self.stream(sid)) and chats:
            self.notes.pop(sid, None)
            queue.put_nowait({"reply": content})
            return
        # Nobody is waiting on this reply, so the pipe writes it into the chat later.
        user = "\n\n".join(_email_text(j) for j in emails) or None
        title = emails[0].email.get("subject", "") if emails else ""
        self.event(sid, "\n\n".join(self.notes.pop(sid, []) + [content]), user, title)


def _email_text(job: Job) -> str:
    """A mail as the chat shows it: subject and body, without the other headers."""
    body = job.prompt.split("\n\n", 1)[-1]
    return f"📧 **{job.email.get('subject', '')}**\n\n{body}"


def run(cfg: Config) -> int:
    if not cfg.users:
        raise SystemExit("taylmail serve: set TAYLMAIL_USERS to the address(es) you mail from")
    if not (cfg.imap and cfg.smtp):
        raise SystemExit("taylmail serve: set TAYLMAIL_IMAP and TAYLMAIL_SMTP")
    if cfg.sender_check not in ("dmarc", "return-path"):
        raise SystemExit("taylmail serve: TAYLMAIL_SENDER_CHECK must be dmarc or return-path")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(Daemon(cfg).main())
    return 0
