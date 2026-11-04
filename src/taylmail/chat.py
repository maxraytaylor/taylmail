"""Chat API: lets a chat frontend's adapter, like openwebui/, talk to sessions.

An adapter names itself (its "client") and its conversations; each
conversation is one session. It posts each message to /turn and streams back
the turn's progress and reply. Turns that came by email, and replies whose
request went away, wait in an event spool until an adapter writes them into
the session's conversation, starting one if it has none.
"""

import asyncio
import hmac
import json
import logging
import re
import time
import uuid
from pathlib import Path

from . import inbox
from .config import Config
from .store import Store

log = logging.getLogger("taylmail.chat")


CLIENT = re.compile(r"[a-z0-9][a-z0-9-]*")


def thread_key(client: str, conversation: str) -> str:
    return f"{client}/{conversation}"


def handle(cfg: Config, client: str, conversation: str, user: str, text: str) -> Path:
    """Queue a chat message for its conversation's session; returns the job's path."""
    store = Store(cfg.home)
    key = thread_key(client, conversation)
    session_id = store.thread_session(key)
    if session_id is None:
        session_id = str(uuid.uuid4())
        store.update_session(session_id, started=False, cwd=str(cfg.cwd))
    target = {"client": client, "conversation": conversation}
    store.link_thread(key, session_id, target)
    return inbox.put(cfg, session_id, "chat", f"From: {user}\n\n{text}", chat=target)


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


def events(cfg: Config, client: str) -> list[dict]:
    """Waiting events, oldest first, each with its session's conversation in `client`."""
    sessions = Store(cfg.home)._read()["sessions"]
    found = []
    for path in sorted(_events(cfg).glob("*.json")):
        event = json.loads(path.read_text())
        target = sessions.get(event["session"], {}).get("chat") or {}
        conversation = target.get("conversation") if target.get("client") == client else None
        found.append({**event, "id": path.stem, "conversation": conversation})
    return found


def ack(cfg: Config, event_id: str, client: str, conversation: str | None) -> bool:
    """Drop a delivered event; `conversation` is where it went."""
    path = _events(cfg) / f"{Path(event_id).name}.json"
    try:
        event = json.loads(path.read_text())
    except FileNotFoundError:
        return False
    if conversation:
        target = {"client": client, "conversation": conversation}
        Store(cfg.home).link_thread(thread_key(client, conversation), event["session"], target)
    path.unlink()
    return True


# --- HTTP API for adapters ---------------------------------------------------


def make_server(cfg: Config, daemon):
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import StreamingResponse

    key = cfg.chat_api_key_file.read_text().strip()

    def auth(request: Request) -> None:
        got = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(got.encode(), key.encode()):
            raise HTTPException(401)

    server = FastAPI(dependencies=[Depends(auth)])

    def client_name(value) -> str:
        if not isinstance(value, str) or not CLIENT.fullmatch(value):
            raise HTTPException(400, "client must be lowercase letters, digits and dashes")
        return value

    @server.post("/turn")
    async def turn(request: Request):
        body = await request.json()
        text = (body.get("text") or "").strip()
        client = client_name(body.get("client"))
        if not body.get("conversation") or not text:
            raise HTTPException(400, "need conversation and text")
        queue: asyncio.Queue = asyncio.Queue()
        job = handle(cfg, client, str(body["conversation"]), body.get("user") or "the user", text)
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
    async def get_events(client: str, wait: float = 0):
        """Long-polls for up to `wait` seconds."""
        client = client_name(client)
        deadline = time.monotonic() + min(wait, 300)
        while True:
            daemon.events_ready.clear()
            if (found := events(cfg, client)) or time.monotonic() >= deadline:
                return {"events": found}
            try:
                await asyncio.wait_for(daemon.events_ready.wait(), deadline - time.monotonic())
            except TimeoutError:
                pass

    @server.post("/events/{event_id}/ack")
    async def post_ack(event_id: str, request: Request):
        body = await request.json()
        conversation = body.get("conversation")
        if not ack(cfg, event_id, client_name(body.get("client")), conversation and str(conversation)):
            raise HTTPException(404)
        return {}

    return server
