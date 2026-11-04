"""
title: Claude (taylmail)
description: Chat with the taylmail agent. Each chat is one session; sessions started by email show up as chats too.
version: 0.1.0
license: MIT
"""

import asyncio
import json
import logging
import sys
import time
import types
import uuid

import aiohttp
from pydantic import BaseModel, Field

from open_webui.models.chats import ChatForm, Chats
from open_webui.models.functions import Functions
from open_webui.models.users import Users

log = logging.getLogger("taylmail.pipe")

# Open WebUI loads this file anew on every change, so state outlives modules here.
_STATE = sys.modules.setdefault("taylmail_pipe_state", types.ModuleType("taylmail_pipe_state"))
FUNCTION_ID = __name__.removeprefix("function_")[:-33]  # function_{id}_{uuid hex}


class Pipe:
    class Valves(BaseModel):
        TAYLMAIL_URL: str = Field("http://127.0.0.1:8000", description="taylmail's chat API")
        TAYLMAIL_KEY: str = Field("", description="the contents of taylmail's chat_api_key")
        OWNER_EMAIL: str = Field("", description="who may chat and gets mail sessions; empty: the first admin")

    def __init__(self):
        self.valves = self.Valves()
        _start()

    async def pipe(self, body: dict, __user__: dict, __chat_id__: str | None = None,
                   __task__: str | None = None, __task_body__: dict | None = None,
                   __event_emitter__=None):
        if __task__ == "title_generation":
            # Open WebUI falls back to the reply as the title, so name the chat here.
            first = next((m for m in (__task_body__ or {}).get("messages", []) if m["role"] == "user"), {})
            return json.dumps({"title": " ".join(_text(first).split())[:60]})
        if __task__:
            return ""  # tags, follow-ups and such: not worth an agent turn
        _start()
        owner = await _owner(self.valves)
        if not owner or __user__.get("id") != owner.id:
            return "This agent only talks to its owner; see OWNER_EMAIL."
        if not __chat_id__ or __chat_id__.startswith("local:"):
            return "A temporary chat can't hold a session. Turn temporary chat off."
        text = _text(next((m for m in reversed(body["messages"]) if m["role"] == "user"), {}))
        return self._turn(__chat_id__, __user__.get("name") or "", text, __event_emitter__)

    async def _turn(self, chat_id: str, user: str, text: str, emit):
        async def status(description: str, done: bool = False) -> None:
            if emit:
                await emit({"type": "status", "data": {"description": description, "done": done}})

        await status("Waiting for Claude…")
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout, headers=_auth(self.valves)) as session:
            url = self.valves.TAYLMAIL_URL.rstrip("/") + "/turn"
            async with session.post(url, json={"chat_id": chat_id, "user": user, "text": text}) as r:
                r.raise_for_status()
                async for line in r.content:
                    item = json.loads(line)
                    if "status" in item:
                        await status(item["status"])
                    elif "content" in item:
                        yield item["content"]
                    elif "reply" in item:
                        await status("Done", done=True)
                        yield item["reply"]
                        return
        raise RuntimeError("taylmail ended the turn without a reply")


def _text(message: dict) -> str:
    content = message.get("content") or ""
    if isinstance(content, list):  # with attachments: a list of parts
        content = "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
    return content


def _auth(valves) -> dict:
    return {"Authorization": f"Bearer {valves.TAYLMAIL_KEY}"}


async def _owner(valves):
    if valves.OWNER_EMAIL:
        return await Users.get_user_by_email(valves.OWNER_EMAIL)
    return await Users.get_super_admin_user()


# --- mail turns into chats ---------------------------------------------------


def _start() -> None:
    """Make this module's poller the one that runs."""
    if getattr(_STATE, "owner", None) == __name__ and not _STATE.task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # pipe() starts it later
    if not hasattr(_STATE, "lock"):
        _STATE.lock = asyncio.Lock()
    _STATE.owner = __name__
    _STATE.task = loop.create_task(_poll())


async def _poll() -> None:
    while _STATE.owner == __name__:
        try:
            function = await Functions.get_function_by_id(FUNCTION_ID)
            if function is None:
                return  # deleted
            valves = Pipe.Valves(**{
                k: v for k, v in (await Functions.get_function_valves_by_id(FUNCTION_ID) or {}).items()
                if v is not None
            })
            if not function.is_active or not valves.TAYLMAIL_KEY:
                await asyncio.sleep(30)
                continue
            base = valves.TAYLMAIL_URL.rstrip("/")
            async with aiohttp.ClientSession(headers=_auth(valves)) as session:
                async with session.get(f"{base}/events", params={"wait": "60"},
                                       timeout=aiohttp.ClientTimeout(total=90)) as r:
                    r.raise_for_status()
                    events = (await r.json())["events"]
                # One writer at a time, or a replaced poller writes events twice.
                async with _STATE.lock:
                    if _STATE.owner != __name__:
                        return
                    chats: dict[str, str] = {}  # session -> chat made in this batch
                    for event in events:
                        chat_id = await _deliver(valves, event, chats.get(event["session"]))
                        async with session.post(f"{base}/events/{event['id']}/ack",
                                                json={"chat_id": chat_id}) as r:
                            r.raise_for_status()
                        chats[event["session"]] = chat_id
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("taylmail events: %s", e)
            await asyncio.sleep(10)


async def _deliver(valves, event: dict, made: str | None) -> str:
    """Write an event into its session's chat, or a new one; returns the chat ID."""
    chat_id = event.get("chat_id") or made
    chat = await Chats.get_chat_by_id(chat_id) if chat_id else None
    now = int(time.time())
    messages = []
    if event.get("user"):
        messages.append({"role": "user", "content": event["user"], "models": [FUNCTION_ID]})
    messages.append({"role": "assistant", "content": event["reply"], "model": FUNCTION_ID, "done": True})
    for m in messages:
        m.update(id=str(uuid.uuid4()), childrenIds=[], timestamp=now)

    if chat is None:
        owner = await _owner(valves)
        if owner is None:
            raise RuntimeError(f"no Open WebUI user {valves.OWNER_EMAIL!r}")
        for prev, m in zip([None] + messages, messages):
            m["parentId"] = prev and prev["id"]
            if prev:
                prev["childrenIds"] = [m["id"]]
        parent = messages[-1]["id"]
        chat = await Chats.insert_new_chat(str(uuid.uuid4()), owner.id, ChatForm(chat={
            "title": event.get("title") or "Mail",
            "models": [FUNCTION_ID],
            "history": {"currentId": parent, "messages": {m["id"]: m for m in messages}},
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        }))
        if chat is None:
            raise RuntimeError("Open WebUI didn't create the chat")
        await _emit(chat.user_id, chat.id, parent, "chat:list")
        return chat.id

    parent = (chat.chat.get("history") or {}).get("currentId")
    for m in messages:
        m["parentId"] = parent
        await Chats.upsert_message_to_chat_by_id_and_message_id(chat.id, m["id"], m)
        parent = m["id"]
    await _emit(chat.user_id, chat.id, parent, "chat:reload")
    await _emit(chat.user_id, chat.id, parent, "chat:list")
    return chat.id


async def _emit(user_id: str, chat_id: str, message_id: str, kind: str) -> None:
    from open_webui.socket.main import sio

    await sio.emit("events", {"chat_id": chat_id, "message_id": message_id, "data": {"type": kind}},
                   room=f"user:{user_id}")
