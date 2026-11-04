"""Chat adapter: an Open WebUI Channel App.

Each channel thread is one session. A top-level message starts a session and
the agent answers in that message's thread; messages in the thread continue
it. Open WebUI connects to us, so it can stay on a private network.
"""

import logging
import re
import time
import uuid

from oix import App

from . import inbox
from .config import Config
from .store import Store

log = logging.getLogger("taylmail.chat")

MENTION = re.compile(r"<@A:[^|>]+\|([^>]*)>")
MAX_CONTENT = 32_000  # Open WebUI's message limit


def make_app(cfg: Config, wake) -> App:
    """`wake` is called after a message is queued."""
    app = App(
        manifest_id="taylmail",
        name="Claude",
        description="A Claude Code agent you can also reach by email.",
        scopes=["channels:messages:read", "channels:messages:write"],
        api_key=cfg.chat_api_key_file.read_text().strip(),
        private_key=cfg.chat_private_key_file.read_text(),
    )

    # background=False: queue durably before Open WebUI gets its receipt.
    @app.on("channel.message.created", background=False)
    async def on_message(event):
        msg = event.message or {}
        author = msg.get("author") or {}
        if author.get("type") != "user":
            return
        if cfg.chat_users and author.get("id") not in cfg.chat_users:
            log.warning("ignoring Open WebUI user %s: not in TAYLMAIL_CHAT_USERS", author.get("id"))
            return
        text = MENTION.sub(r"@\1", event.content or "").strip()
        if not text:
            return
        root = event.parent_id or event.message_id
        key = f"{event.channel_id}/{root}"
        store = Store(cfg.home)
        session_id = store.thread_session(key)
        if session_id is None:
            session_id = str(uuid.uuid4())
            store.update_session(session_id, started=False, cwd=str(cfg.cwd))
        target = {
            "installation_id": event.installation_id,
            "channel_id": event.channel_id,
            "thread_id": root,
        }
        store.link_thread(key, session_id, target)
        name = (event.actor or {}).get("name") or author.get("id")
        inbox.put(cfg, session_id, "chat", f"From: {name}\n\n{text}", chat=target)
        wake()

    return app


class Chat:
    """Posting into session threads, outside of any event handler."""

    def __init__(self, app: App):
        self.app = app

    async def post(self, target: dict, content: str) -> dict:
        client = self.app.client(target["installation_id"])
        return await client.create_message(
            target["channel_id"], content=content[:MAX_CONTENT], parent_id=target["thread_id"]
        )

    async def status(self, target: dict) -> "Status":
        return Status(self, target, await self.post(target, "⏳ Working…"))


class Status:
    """A message that shows the turn's progress, then becomes its final reply."""

    def __init__(self, chat: Chat, target: dict, message: dict):
        self.chat, self.target, self.message = chat, target, message
        self.lines: list[str] = []
        self.last_edit = 0.0

    async def _edit(self, content: str) -> None:
        client = self.chat.app.client(self.target["installation_id"])
        self.message = await client.update_message(
            self.message["id"], content=content[:MAX_CONTENT], etag=self.message["etag"]
        )

    async def progress(self, line: str) -> None:
        self.lines = (self.lines + [line])[-5:]
        if time.monotonic() - self.last_edit < 2:
            return  # don't edit on every tool call
        self.last_edit = time.monotonic()
        try:
            lines = [line.replace("`", "'") for line in self.lines]
            await self._edit("⏳ Working…\n\n" + "\n".join(f"- `{line}`" for line in lines))
        except Exception as e:  # progress is cosmetic
            log.warning("status edit failed: %s", e)

    async def done(self, content: str) -> None:
        await self._edit(content)
