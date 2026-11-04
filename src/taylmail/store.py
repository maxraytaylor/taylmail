"""Tiny JSON state file: which Message-ID or chat thread belongs to which session.

For email, the plus-address is the primary way to find a session; the
Message-ID map is the fallback for when a mail client drops it (e.g.
reply-all to a rewritten To). Chat threads are keyed "channel/root-message".
"""

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, home: Path):
        self.home = home
        self.path = home / "state.json"
        home.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self):
        with open(self.home / "state.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self._read()
            yield state
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2))
            tmp.replace(self.path)

    def _read(self) -> dict:
        try:
            state = json.loads(self.path.read_text())
        except FileNotFoundError:
            state = {}
        state.setdefault("messages", {})
        state.setdefault("sessions", {})
        state.setdefault("threads", {})
        state.setdefault("chat", {})
        state.setdefault("chat_names", {})
        return state

    def session(self, session_id: str) -> dict | None:
        return self._read()["sessions"].get(session_id)

    def update_session(self, session_id: str, **fields) -> dict:
        with self._locked() as state:
            info = state["sessions"].setdefault(session_id, {"sent": 0})
            info.update(fields)
            return dict(info)

    def record_message(self, message_id: str, session_id: str, *, sent: bool) -> None:
        """Remember a Message-ID; `sent` means the agent sent it to the user."""
        with self._locked() as state:
            state["messages"][message_id] = session_id
            if sent:
                info = state["sessions"].setdefault(session_id, {"sent": 0})
                info["sent"] = info.get("sent", 0) + 1

    def lookup(self, message_ids: list[str]) -> str | None:
        """Session of the most recent known message among `message_ids`."""
        messages = self._read()["messages"]
        for mid in reversed(message_ids):
            if mid in messages:
                return messages[mid]
        return None

    def thread_session(self, key: str) -> str | None:
        return self._read()["threads"].get(key)

    def link_thread(self, key: str, session_id: str, chat: dict) -> None:
        """Tie a chat thread to a session; `chat` says where to post replies."""
        with self._locked() as state:
            state["threads"][key] = session_id
            info = state["sessions"].setdefault(session_id, {"sent": 0})
            info["chat"] = chat

    def chat_threads(self, channel_id: str) -> list[str]:
        """Root message IDs of the channel's session threads."""
        prefix = channel_id + "/"
        return [k[len(prefix):] for k in self._read()["threads"] if k.startswith(prefix)]

    def chat_after(self, channel_id: str) -> int | None:
        """created_at (ns) of the newest message handled in the channel."""
        return self._read()["chat"].get(channel_id, {}).get("after")

    def chat_seen(self, channel_id: str, message_id: str, created_at: int) -> bool:
        """Mark a chat message handled. False if it already was."""
        with self._locked() as state:
            info = state["chat"].setdefault(channel_id, {"after": created_at, "seen": []})
            if message_id in info["seen"]:
                return False
            info["seen"] = info["seen"][-(SEEN - 1):] + [message_id]
            info["after"] = max(info["after"], created_at)
            return True

    def chat_name(self, user_id: str, name: str | None = None) -> str:
        """Remember an Open WebUI user's name, or recall it."""
        if name:
            with self._locked() as state:
                state["chat_names"][user_id] = name
            return name
        return self._read()["chat_names"].get(user_id, user_id)


SEEN = 500  # message IDs remembered per channel, against events racing catch-up
