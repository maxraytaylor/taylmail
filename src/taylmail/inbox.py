"""Spool of user messages waiting for an agent turn.

Both adapters write here before acknowledging a message, so nothing is lost
if the daemon dies mid-turn; the turn simply runs again on restart.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config


@dataclass
class Job:
    path: Path
    session: str
    source: str  # "email" or "chat"
    prompt: str
    email: dict = field(default_factory=dict)  # headers needed to reply
    chat: dict = field(default_factory=dict)  # where to reply in Open WebUI


def _dir(cfg: Config) -> Path:
    d = cfg.home / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def put(cfg: Config, session: str, source: str, prompt: str, **ctx) -> Path:
    d = _dir(cfg)
    path = d / f"{time.time_ns()}-{session}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"session": session, "source": source, "prompt": prompt, **ctx}))
    tmp.replace(path)
    return path


def pending(cfg: Config) -> dict[str, list[Job]]:
    """Waiting jobs by session, oldest first."""
    jobs: dict[str, list[Job]] = {}
    for path in sorted(_dir(cfg).glob("*.json")):
        data = json.loads(path.read_text())
        jobs.setdefault(data["session"], []).append(Job(path=path, **data))
    return jobs
