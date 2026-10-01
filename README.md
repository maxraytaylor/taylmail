# taylmail

Talk to a Claude Code agent over email, the way you'd talk to a contributor
on a mailing list. The agent sends patch series and you reply inline in aerc.
Each reply resumes the same agent session.

Everything runs on one machine and no mail server is involved:

```
you (aerc) ──outgoing──▶ taylmail u2a ──▶ agent session (Claude Agent SDK)
                                              │
aerc ◀── Maildir ◀── taylmail a2u ◀── git send-email --sendmail-cmd
```

- **`taylmail u2a`** (user to agent) is aerc's `outgoing` command. It reads
  your mail, finds the session, then starts the agent turn in the background
  and returns immediately.
- **`taylmail a2u`** (agent to user) is a sendmail stand-in for
  `git send-email --sendmail-cmd`. It writes the mail into the Maildir that
  aerc reads.

The agent writes as `Claude <claude+SESSION@taylmail.localhost>`, so when you
hit Reply the mail is addressed back to that session. If the plus-address
gets lost, u2a falls back to looking up `In-Reply-To`/`References` in its
Message-ID map. Mail to plain `claude@taylmail.localhost` starts a new
session.

If the agent sends no mail during a turn, its final message is mailed back
to you as an ordinary reply. Use that for questions and discussion. Errors
get mailed back the same way.

## Install

```nu
cd ~/src/vibetaylmail
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Try it

```nu
dev/aerc           # or: dev/aerc --reset   to start from scratch
```

This starts aerc against a sandbox in `dev/sandbox/`, which holds a Maildir,
the taylmail state and a small repo with a bug in it. Your own aerc config is
not touched. To start, compose to `claude@taylmail.localhost`.

## aerc account

`~/.config/aerc/accounts.conf`:

```ini
[taylmail]
source   = maildir://~/Maildir/taylmail
default  = INBOX
from     = You <you@example.com>
outgoing = env TAYLMAIL_CWD=/home/you/src/project /home/you/src/vibetaylmail/.venv/bin/taylmail u2a
```

Use absolute paths, because `env` doesn't expand `~`. Your `from` address
needs a dotted domain: `git send-email` rejects `you@localhost`.

## Settings (environment variables)

| Variable | Default | |
|---|---|---|
| `TAYLMAIL_CWD` | current directory | where the agent works; a git repo |
| `TAYLMAIL_MAILDIR` | `~/Maildir/taylmail/INBOX` | the folder aerc reads |
| `TAYLMAIL_HOME` | `~/.local/share/taylmail` | state, spool, per-session logs |
| `TAYLMAIL_DOMAIN` | `taylmail.localhost` | domain of the agent's address |
| `TAYLMAIL_CLAUDE` | `claude` on `PATH` | CLI the SDK drives (the pip-bundled one doesn't run on NixOS) |
| `TAYLMAIL_PERMISSION_MODE` | `auto` | SDK permission mode; `auto` lets a classifier approve each tool call. `git` is always allowed |
| `TAYLMAIL_MODEL` | CLI default | |

The working directory is fixed when a session starts, and later replies keep
using it.

## Debugging

```nu
# run a turn in the foreground instead of in the background
open --raw mail.eml | .venv/bin/taylmail u2a --foreground
# background turns log here
tail -f ~/.local/share/taylmail/logs/*.log
# tests
.venv/bin/python -m unittest discover tests
```

## Known limits (prototype)

- Only one turn runs per session at a time. A second reply waits for the
  first to finish.
- Nobody is there to answer permission prompts, so any tool call the `auto`
  classifier denies stays denied. If the agent sent nothing that turn, the
  denial is listed in its reply.
- Only the `text/plain` part of your mail is passed to the agent.
  Attachments are ignored.
