"""taylmail serve: one process that owns every session.

Adapters queue user messages into the inbox; the scheduler runs one turn per
session at a time, batching whatever arrived while the previous turn ran.
"""

import asyncio
import logging
from email.message import EmailMessage

from . import agent, inbox, mail
from .config import Config
from .inbox import Job
from .store import Store

log = logging.getLogger("taylmail")


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.wakeup = asyncio.Event()
        self.busy: set[str] = set()
        self.tasks: set[asyncio.Task] = set()  # keep running turns referenced
        self.chat = None

    def wake(self) -> None:
        self.wakeup.set()

    async def main(self) -> None:
        cfg = self.cfg
        tasks = [self.scheduler(), self.outbox_loop()]
        if cfg.imap:
            tasks.append(self.imap_loop())
        if cfg.chat_api_key_file:
            tasks.append(self.serve_chat())
        log.info(
            "serving %s: mail %s, chat %s", cfg.address,
            f"IMAP {cfg.imap.host}" if cfg.imap else f"local Maildir {cfg.maildir}",
            cfg.chat_listen if cfg.chat_api_key_file else "off",
        )
        await asyncio.gather(*tasks)

    # --- adapters ------------------------------------------------------------

    async def imap_loop(self) -> None:
        def handle(raw: bytes) -> None:
            if sid := mail.intake(self.cfg, raw, trusted=False):
                log.info("mail for session %s", sid)

        while True:
            try:
                if await asyncio.to_thread(mail.imap_fetch, self.cfg, handle):
                    self.wake()
            except Exception as e:
                log.warning("IMAP poll failed: %s", e)
            await asyncio.sleep(self.cfg.poll_seconds)

    async def serve_chat(self) -> None:
        import uvicorn
        from fastapi import FastAPI
        from oix.fastapi import create_router

        from .chat import Chat, make_app

        app = make_app(self.cfg, self.wake)
        self.chat = Chat(app)
        server = FastAPI()
        server.include_router(create_router(app, path="/events"))
        host, _, port = self.cfg.chat_listen.rpartition(":")
        config = uvicorn.Config(
            server, host=host, port=int(port), ws_max_size=16 * 1024 * 1024, log_level="warning"
        )
        await uvicorn.Server(config).serve()

    async def outbox_loop(self) -> None:
        while True:
            await self.flush_outbox()
            await asyncio.sleep(1)

    async def flush_outbox(self) -> None:
        try:
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
            target = (Store(self.cfg.home).session(sid) or {}).get("chat")
            if target and self.chat:
                lines = "\n".join(f"- {m['Subject']}" for m in msgs)
                await self._post(target, f"📧 Sent to your inbox:\n{lines}")

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
            # u2a queues from another process, so also look every second.
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=1)
            except TimeoutError:
                pass

    async def turn(self, sid: str, jobs: list[Job]) -> None:
        cfg, store = self.cfg, Store(self.cfg.home)
        try:
            emails = [j for j in jobs if j.source == "email"]
            chats = [j for j in jobs if j.source == "chat"]
            status = None
            if chats and self.chat:
                try:
                    status = await self.chat.status(chats[-1].chat)
                except Exception as e:
                    log.warning("chat status failed: %s", e)
            log.info("session %s: turn with %d message(s)", sid, len(jobs))

            sent_before = (store.session(sid) or {}).get("sent", 0)
            error = None
            try:
                result = await agent.run_turn(
                    cfg, sid, jobs, progress=status.progress if status else None
                )
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
            if chats:
                content = body.strip() or "Done."
                if status:
                    await status.done(content)
                else:
                    await self._post(chats[-1].chat, content)

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

    async def _post(self, target: dict, content: str) -> None:
        if not self.chat:
            return
        try:
            await self.chat.post(target, content)
        except Exception as e:
            log.warning("chat post failed: %s", e)


def run(cfg: Config) -> int:
    if not cfg.users:
        raise SystemExit("taylmail serve: set TAYLMAIL_USERS to the address(es) you mail from")
    if cfg.chat_api_key_file and not cfg.chat_private_key_file:
        raise SystemExit("taylmail serve: set TAYLMAIL_CHAT_PRIVATE_KEY_FILE too")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(Daemon(cfg).main())
    return 0
