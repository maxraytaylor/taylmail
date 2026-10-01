"""Run one agent turn for a spooled user email."""

import asyncio
import email
import email.policy
import fcntl
import os
import shlex
import sys
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .a2u import deliver
from .config import Config
from .store import Store

SYSTEM_PROMPT = """\
You are talking to the user over email, like a contributor on a mailing list.
Each user turn is an email from them; reply the way a good kernel-style
contributor would.

How your words reach the user:
- Code changes go out as a patch series. Commit your work in {cwd} as clean,
  reviewable commits, then send them with exactly this command (add a
  revision range or patch files at the end):

    {send_email}

  For more than one patch, use `git format-patch --cover-letter -o <dir> <range>`,
  fill in the cover letter's subject and blurb, then `{send_email} <dir>/*.patch`.
  Use `-v2`, `-v3`… on resends. Don't use --compose (no editor available).
- If you sent mail this turn, your final message is NOT emailed; keep it short.
- If you sent nothing, your final message IS emailed to the user as a plain
  reply to their mail. Write it as an email body: no headers, no sign-off
  needed. Quote what you respond to with "> " when that helps.

The user reviews by replying inline to patches. Their reply is your next turn.
"""


def send_email_cmd(cfg: Config, session_id: str, user: str, in_reply_to: str | None) -> str:
    taylmail = Path(sys.executable).with_name("taylmail")
    args = [
        "git", "send-email",
        f"--sendmail-cmd={shlex.quote(str(taylmail))} a2u",
        f"--from={cfg.agent_from(session_id)}",
        f"--to={user}",
        "--confirm=never", "--suppress-cc=all", "--quiet",
    ]
    if in_reply_to:
        args.append(f"--in-reply-to={in_reply_to}")
    return shlex.join(args)


def render_prompt(msg: EmailMessage) -> str:
    body_part = msg.get_body(preferencelist=("plain",))
    body = body_part.get_content() if body_part else "(no text/plain body)"
    head = "\n".join(
        f"{name}: {msg[name]}"
        for name in ("From", "Date", "Subject", "Message-ID", "In-Reply-To")
        if msg[name]
    )
    return f"{head}\n\n{body}"


def reply_to(cfg: Config, original: EmailMessage, session_id: str, body: str) -> EmailMessage:
    reply = EmailMessage()
    subject = original["Subject"] or ""
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    reply["From"] = cfg.agent_from(session_id)
    reply["To"] = original["Reply-To"] or original["From"]
    reply["Message-ID"] = make_msgid(domain=cfg.domain)
    if mid := original["Message-ID"]:
        reply["In-Reply-To"] = mid
        reply["References"] = " ".join((original["References"] or "").split() + [mid])
    reply.set_content(body)
    return reply


async def run_turn(cfg: Config, session_id: str, msg: EmailMessage) -> ResultMessage | None:
    store = Store(cfg.home)
    info = store.session(session_id) or {}
    cwd = info.get("cwd", str(cfg.cwd))
    user = info.get("user") or msg["From"]
    mid = (msg["Message-ID"] or "").strip() or None

    bin_dir = str(Path(sys.executable).parent)
    options = ClaudeAgentOptions(
        cli_path=cfg.claude_cli,
        cwd=cwd,
        model=cfg.model,
        permission_mode=cfg.permission_mode,
        # git send-email must work headless, whatever the permission mode.
        allowed_tools=["Bash(git:*)", "Bash(taylmail:*)", f"Bash({bin_dir}/taylmail:*)"],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": SYSTEM_PROMPT.format(
                cwd=cwd, send_email=send_email_cmd(cfg, session_id, user, mid)
            ),
        },
        env={
            "TAYLMAIL_SESSION": session_id,
            "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
        },
        **({"resume": session_id} if info.get("started") else {"session_id": session_id}),
    )

    result = None
    async for message in query(prompt=render_prompt(msg), options=options):
        if isinstance(message, ResultMessage):
            result = message
    store.update_session(session_id, started=True)
    return result


def run(cfg: Config, path: str) -> int:
    msg = email.message_from_bytes(Path(path).read_bytes(), policy=email.policy.default)
    session_id = Path(path).stem.split("-", 1)[1]
    store = Store(cfg.home)

    # One turn at a time per session: two quick replies must not both resume.
    locks = cfg.home / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with open(locks / session_id, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        sent_before = (store.session(session_id) or {}).get("sent", 0)
        try:
            result = asyncio.run(run_turn(cfg, session_id, msg))
        except Exception as e:  # tell the user rather than failing silently
            result, error = None, f"{type(e).__name__}: {e}"
        else:
            error = None
        sent_after = (store.session(session_id) or {}).get("sent", 0)

        if result is not None and result.is_error:
            error = f"agent stopped: {result.subtype}\n" + "\n".join(result.errors or [])
        if error:
            body = f"[taylmail] This turn failed.\n\n{error}\n"
        elif sent_after == sent_before and result is not None and result.result:
            body = result.result
        else:
            body = None
        if result is not None and result.permission_denials:
            denied = "\n".join(f"- {d}" for d in result.permission_denials)
            body = (body or "") + f"\n\n[taylmail] Permission denied for:\n{denied}\n"
        if body:
            deliver(cfg, reply_to(cfg, msg, session_id, body), session_id=session_id)

    Path(path).unlink()
    print(f"taylmail run: session {session_id} done", file=sys.stderr)
    return 0 if not error else 1
