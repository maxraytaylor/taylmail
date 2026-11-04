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
from oix.socket import NAMESPACE

from . import inbox
from .config import Config
from .store import Store

log = logging.getLogger("taylmail.chat")

MENTION = re.compile(r"<@A:[^|>]+\|([^>]*)>")
MAX_CONTENT = 32_000  # Open WebUI's message limit
RACE_NS = 60 * 10**9


def make_app(cfg: Config, wake) -> App:
    """`wake` is called after a message is queued."""
    app = App(
        manifest_id="taylmail",
        name="Claude",
        description="A Claude Code agent you can also reach by email.",
        scopes=["channels:read", "channels:messages:read", "channels:messages:write"],
        api_key=cfg.chat_api_key_file.read_text().strip(),
        private_key=cfg.chat_private_key_file.read_text(),
    )

    # background=False: queue durably before Open WebUI gets its receipt.
    @app.on("channel.message.created", background=False)
    async def on_message(event):
        handle(cfg, wake, event.installation_id, event.message or {}, (event.actor or {}).get("name"))

    return app


def handle(cfg: Config, wake, installation_id: str, msg: dict, name: str | None = None) -> bool:
    """Queue a channel message for its thread's session; False if skipped."""
    author = msg.get("author") or {}
    if author.get("type") != "user":
        return False
    if cfg.chat_users and author.get("id") not in cfg.chat_users:
        log.warning("ignoring Open WebUI user %s: not in TAYLMAIL_CHAT_USERS", author.get("id"))
        return False
    text = MENTION.sub(r"@\1", msg.get("content") or "").strip()
    if not text:
        return False
    channel = msg["channel_id"]
    store = Store(cfg.home)
    if not store.chat_seen(channel, msg["id"], int(msg["created_at"])):
        return False
    root = msg.get("parent_id") or msg["id"]
    key = f"{channel}/{root}"
    session_id = store.thread_session(key)
    if session_id is None:
        session_id = str(uuid.uuid4())
        store.update_session(session_id, started=False, cwd=str(cfg.cwd))
    target = {"installation_id": installation_id, "channel_id": channel, "thread_id": root}
    store.link_thread(key, session_id, target)
    name = store.chat_name(author["id"], name)
    inbox.put(cfg, session_id, "chat", f"From: {name}\n\n{text}", chat=target)
    wake()
    return True


async def catch_up(cfg: Config, wake, client) -> None:
    """Queue messages posted while we were away; Open WebUI sends each event once."""
    store = Store(cfg.home)
    missed = []
    for channel in await client.list_channels():
        cid = channel["id"]
        after = store.chat_after(cid)
        if after is None:
            continue  # nothing handled here yet, so no history to answer
        top = await _since(client, cid, None, after)
        missed += top
        for root in {m["id"] for m in top} | set(store.chat_threads(cid)):
            missed += await _since(client, cid, root, after)
    queued = sum(
        handle(cfg, wake, client.installation_id, msg)
        for msg in sorted(missed, key=lambda m: int(m["created_at"]))
    )
    if queued:
        log.info("caught up on %d chat messages", queued)


async def _since(client, channel_id: str, parent_id: str | None, after: int) -> list[dict]:
    """Messages newer than `after`, less a margin for events that raced each other."""
    found, before = [], None
    while True:
        page = await client.list_messages(channel_id, limit=100, before=before, parent_id=parent_id)
        for msg in page["messages"]:
            if int(msg["created_at"]) < after - RACE_NS:
                return found
            found.append(msg)
        if not (before := page["next_cursor"]):
            return found


def on_ready(app: App, callback) -> None:
    """Call `callback(installation_id)` each time Open WebUI connects.

    oix 0.8 has no hook for this, so wrap its socket's app:ready handler.
    """
    sock = app._socket
    ready = sock.ready

    async def wrapped(sid, data):
        result = await ready(sid, data)
        if result.get("status") == 200:
            callback(sock.peers[sid]["installation_id"])
        return result

    sock.server.on("app:ready", wrapped, namespace=NAMESPACE)


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
