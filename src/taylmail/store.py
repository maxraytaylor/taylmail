"""Tiny JSON state file: which Message-ID belongs to which session.

The plus-address is the primary way to find a session; this map is the
fallback for when a mail client drops it (e.g. reply-all to a rewritten To).
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
