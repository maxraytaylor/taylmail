"""Chat adapter: Open WebUI's regular chats, through the pipe in openwebui/.

Each Open WebUI chat is one session. The pipe posts each chat message to
/turn and streams back the turn's progress and reply. Turns that came by
email, and replies whose request went away, wait in an event spool until the
pipe writes them into the session's chat, starting a chat if it has none.
"""

import asyncio
import hmac
import json
import logging
import time
import uuid
from pathlib import Path

from . import inbox
from .config import Config
from .store import Store

log = logging.getLogger("taylmail.chat")


def thread_key(chat_id: str) -> str:
    return f"owui/{chat_id}"


def handle(cfg: Config, chat_id: str, user: str, text: str) -> Path:
    """Queue a chat message for its chat's session; returns the job's path."""
    store = Store(cfg.home)
    key = thread_key(chat_id)
    session_id = store.thread_session(key)
    if session_id is None:
        session_id = str(uuid.uuid4())
        store.update_session(session_id, started=False, cwd=str(cfg.cwd))
    store.link_thread(key, session_id, {"chat_id": chat_id})
    return inbox.put(cfg, session_id, "chat", f"From: {user}\n\n{text}", chat={"chat_id": chat_id})


# --- event spool -------------------------------------------------------------


def _events(cfg: Config) -> Path:
    d = cfg.home / "events"
    d.mkdir(parents=True, exist_ok=True)
    return d


def put_event(cfg: Config, session: str, reply: str, user: str | None = None, title: str = "") -> None:
    """Something for the session's chat: `user` is a message to show before `reply`."""
    path = _events(cfg) / f"{time.time_ns()}-{session}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"session": session, "title": title, "user": user, "reply": reply}))
    tmp.replace(path)


def events(cfg: Config) -> list[dict]:
    """Waiting events, oldest first, each with its session's chat if it has one."""
    sessions = Store(cfg.home)._read()["sessions"]
    found = []
    for path in sorted(_events(cfg).glob("*.json")):
        event = json.loads(path.read_text())
        chat = sessions.get(event["session"], {}).get("chat") or {}
        found.append({**event, "id": path.stem, "chat_id": chat.get("chat_id")})
    return found


def ack(cfg: Config, event_id: str, chat_id: str | None) -> bool:
    """Drop a delivered event; `chat_id` is the chat it went to."""
    path = _events(cfg) / f"{Path(event_id).name}.json"
    try:
        event = json.loads(path.read_text())
    except FileNotFoundError:
        return False
    if chat_id:
        Store(cfg.home).link_thread(thread_key(chat_id), event["session"], {"chat_id": chat_id})
    path.unlink()
    return True


# --- HTTP API for the pipe ---------------------------------------------------


def make_server(cfg: Config, daemon):
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import StreamingResponse

    key = cfg.chat_api_key_file.read_text().strip()

    def auth(request: Request) -> None:
        got = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(got.encode(), key.encode()):
            raise HTTPException(401)

    server = FastAPI(dependencies=[Depends(auth)])

    @server.post("/turn")
    async def turn(request: Request):
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not body.get("chat_id") or not text:
            raise HTTPException(400, "need chat_id and text")
        queue: asyncio.Queue = asyncio.Queue()
        job = handle(cfg, body["chat_id"], body.get("user") or "the user", text)
        daemon.waiters[job.name] = queue
        daemon.wake()

        async def stream():
            try:
                while True:
                    item = await queue.get()
                    yield json.dumps(item) + "\n"
                    if "reply" in item:
                        return
            finally:
                daemon.waiters.pop(job.name, None)

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @server.get("/events")
    async def get_events(wait: float = 0):
        """Long-polls for up to `wait` seconds."""
        deadline = time.monotonic() + min(wait, 300)
        while True:
            daemon.events_ready.clear()
            if (found := events(cfg)) or time.monotonic() >= deadline:
                return {"events": found}
            try:
                await asyncio.wait_for(daemon.events_ready.wait(), deadline - time.monotonic())
            except TimeoutError:
                pass

    @server.post("/events/{event_id}/ack")
    async def post_ack(event_id: str, request: Request):
        body = await request.json()
        if not ack(cfg, event_id, body.get("chat_id")):
            raise HTTPException(404)
        return {}

    return server
