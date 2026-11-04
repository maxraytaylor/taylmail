"""Run one agent turn over the user messages queued for a session."""

import os
import shlex
import sys
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, ToolUseBlock, query

from .config import Config
from .inbox import Job
from .store import Store

SYSTEM_PROMPT = """\
You are talking to the user like a contributor on a mailing list. They reach
you by email and by chat (an Open WebUI channel thread); both land in this
one session. Each user turn holds one or more of their messages, each marked
with where it came from.

How your words reach the user:
- Code changes always go out by email, as a patch series. Commit your work in
  {cwd} as clean, reviewable commits, then send them with exactly this
  command (add a revision range or patch files at the end):

    {send_email}

  For more than one patch, use `git format-patch --cover-letter -o <dir> <range>`,
  fill in the cover letter's subject and blurb, then `{send_email} <dir>/*.patch`.
  Use `-v2`, `-v3`… on resends. Don't use --compose (no editor available).
  The chat thread is told what you sent.
- Your final message goes back wherever this turn's messages came from: as a
  plain email reply, as a chat message, or both. If you sent mail this turn
  and the turn came by email, your final message is not emailed; keep it short.
- Write the final message so it reads well as an email body and as chat
  markdown: no headers, no sign-off. Quote what you respond to with "> " when
  that helps.

The user reviews by replying inline to patches or by writing in the chat
thread. Their reply is your next turn.
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


def render_prompt(jobs: list[Job]) -> str:
    return "\n\n".join(f"<{job.source}>\n{job.prompt}\n</{job.source}>" for job in jobs)


def describe(block: ToolUseBlock) -> str:
    """One line for the chat status message."""
    inp = block.input or {}
    detail = inp.get("description") or inp.get("command") or inp.get("file_path") or ""
    return f"{block.name}: {str(detail).splitlines()[0][:120]}" if detail else block.name


async def run_turn(cfg: Config, session_id: str, jobs: list[Job], progress=None) -> ResultMessage | None:
    """`progress`, if given, is awaited with a line for each tool call."""
    store = Store(cfg.home)
    info = store.session(session_id) or {}
    cwd = info.get("cwd", str(cfg.cwd))
    user = cfg.user or info.get("user")

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
                cwd=cwd,
                send_email=send_email_cmd(cfg, session_id, user, info.get("last_email")),
            ),
        },
        env={
            "TAYLMAIL_SESSION": session_id,
            "TAYLMAIL_HOME": str(cfg.home),
            "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
        },
        **({"resume": session_id} if info.get("started") else {"session_id": session_id}),
    )

    result = None
    async for message in query(prompt=render_prompt(jobs), options=options):
        if isinstance(message, ResultMessage):
            result = message
        elif progress and isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    await progress(describe(block))
    store.update_session(session_id, started=True)
    return result
