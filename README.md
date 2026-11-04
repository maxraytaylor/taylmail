# taylmail

Talk to a Claude Code agent the way you'd talk to a contributor on a mailing
list. The agent sends patch series and you reply inline in aerc. You can also
chat with the same agent session in Open WebUI, where each chat is a session.

One daemon, `taylmail serve`, owns every session. Adapters queue your
messages, and the daemon runs one agent turn per session at a time:

```
aerc ──SMTP──▶ mail server ──IMAP──▶ ┐
Open WebUI chat ──pipe──▶ POST /turn ┴─▶ inbox spool ─▶ taylmail serve ─▶ agent turn
                                                                              │
aerc ◀──IMAP── mail server ◀──SMTP── outbox ◀── a2u ◀── git send-email ◀──────┘
Open WebUI chat ◀── /turn stream: status, "📧 Sent" notices, final reply
                ◀── pipe ◀── GET /events: mail turns, replies nobody waited for
```

- **Sessions.** The agent writes as `Claude <claude+SESSION@domain>`, so
  hitting Reply goes back to that session. If the plus-address gets lost,
  the `In-Reply-To`/`References` headers are looked up in a Message-ID map.
  Mail to plain `claude@domain` starts a new session. In Open WebUI, a new
  chat starts a session and messages in that chat continue it.
- **Every session has a chat.** Mail that starts a session also starts an
  Open WebUI chat, titled with its subject. Your mails and the agent's
  replies show up there, so you can answer either way.
- **Patches always go by email.** When the agent sends mail, the session's
  chat is told what was sent.
- **Replies go back where the turn came from, and to the chat.** Messages
  that arrive while a turn runs are batched into the next turn. If the turn
  came by email and the agent sent no mail, its final message is mailed back
  as a reply. While a chat turn runs, its status line shows the agent's
  latest tool call.
- **No polling.** The daemon waits on IMAP IDLE, so it sees your mail as
  soon as the server has it (this needs Python 3.14). The pipe long-polls
  for mail turns, and `a2u` wakes the daemon over a Unix socket.
- **Durability.** Adapters write each message into a spool before
  acknowledging it. IMAP marks a mail seen only after queuing it, and the
  pipe's request is answered only after the message is queued. What the
  chat should show waits in an event spool until the pipe has written it.

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

Run `dev/mailserver` first. It starts a throwaway Dovecot IMAP server and
an SMTP sink on localhost. `dev/aerc` then starts `taylmail serve` and an
aerc that use them, with taylmail's state and a small repo with a bug in it
under `dev/sandbox/`. Your own aerc config is not touched. To start, compose
to `claude@taylmail.test`. The daemon logs to `dev/sandbox/serve.log`.

## Mail server

The agent needs its own mailbox on a server that supports plus-addressing.
The daemon reads it over IMAP and sends over SMTP. Your aerc stays an
ordinary mail client.

```nu
$env.TAYLMAIL_ADDRESS = "claude@example.com"
$env.TAYLMAIL_USERS = "you@example.com"
$env.TAYLMAIL_IMAP = "imaps://imap.example.com"
$env.TAYLMAIL_SMTP = "smtps://smtp.example.com"
$env.TAYLMAIL_MAIL_PASSWORD_FILE = "/run/secrets/taylmail-mail"
$env.TAYLMAIL_CWD = "/srv/project"
taylmail serve
```

Only mail from `TAYLMAIL_USERS` is read, and the mail server must vouch for
the sender. By default (`TAYLMAIL_SENDER_CHECK=dmarc`), the topmost
`Authentication-Results` header needs `dmarc=pass` or `dkim=pass` for the
sender's domain. A private server that takes mail only from its own users
adds no such header. With `return-path`, the topmost `Return-Path` must
match `From` instead. Use that only if the server binds the envelope sender
to the login (Postfix: `reject_sender_login_mismatch`), drops `Return-Path`
headers from submitted mail, and doesn't accept mail from the internet. The agent only ever mails
`TAYLMAIL_USERS`, whatever addresses it asks for.

## Chat: Open WebUI

taylmail serves a small API that an Open WebUI [pipe function][pipes],
[`openwebui/taylmail_pipe.py`](openwebui/taylmail_pipe.py), talks to. The
pipe shows up as a model named after the function. It runs inside Open
WebUI, so it can write mail turns into your chats.

1. Make an API key and point the daemon at it:

   ```nu
   .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))' | save ~/.config/taylmail/chat_api_key
   $env.TAYLMAIL_CHAT_API_KEY_FILE = "~/.config/taylmail/chat_api_key"
   ```

2. As an admin, go to Admin Panel → Functions, add a function and paste
   `openwebui/taylmail_pipe.py` into it. Call it `Claude`, for example.
3. In the function's valves, set `TAYLMAIL_KEY` to the key, and
   `TAYLMAIL_URL` if the daemon doesn't listen on `http://127.0.0.1:8000`.
   Then enable the function.
4. Start a new chat with the `Claude` model.

Only one Open WebUI user talks to the agent and gets the chats that mail
starts: the `OWNER_EMAIL` valve, or by default the first admin. Temporary
chats don't work, since they have no ID to tie a session to. The pipe
collects mail turns while Open WebUI runs and its model list has been
loaded, e.g. by opening Open WebUI once. Until then they wait in taylmail's
event spool.

[pipes]: https://docs.openwebui.com/features/extensibility/plugin/functions/pipe

## Settings (environment variables)

| Variable | Default | |
|---|---|---|
| `TAYLMAIL_USERS` | (required) | your address(es); the only senders read and recipients mailed |
| `TAYLMAIL_CWD` | current directory | where the agent works; a git repo |
| `TAYLMAIL_ADDRESS` | `claude@taylmail.localhost` | the agent's address; sessions use `local+SESSION@domain` |
| `TAYLMAIL_HOME` | `~/.local/share/taylmail` | state and spools |
| `TAYLMAIL_IMAP` | (required) | `imaps://host[:port]`, `imap://` (STARTTLS) or `imap+insecure://` |
| `TAYLMAIL_SMTP` | (required) | `smtps://`, `smtp://` (STARTTLS) or `smtp+insecure://` |
| `TAYLMAIL_MAIL_USER` | `TAYLMAIL_ADDRESS` | IMAP/SMTP login |
| `TAYLMAIL_MAIL_PASSWORD_FILE` | | IMAP/SMTP password |
| `TAYLMAIL_POLL_SECONDS` | `15` | IMAP poll interval, only for servers without IDLE or Python before 3.14 |
| `TAYLMAIL_SENDER_CHECK` | `dmarc` | how to trust your From line; see [Mail server](#mail-server) |
| `TAYLMAIL_CHAT_LISTEN` | `127.0.0.1:8000` | where the API for the Open WebUI pipe listens |
| `TAYLMAIL_CHAT_API_KEY_FILE` | (chat off) | bearer key the pipe presents |
| `TAYLMAIL_CLAUDE` | `claude` on `PATH` | CLI the SDK drives (the pip-bundled one doesn't run on NixOS) |
| `TAYLMAIL_PERMISSION_MODE` | `auto` | SDK permission mode; `auto` lets a classifier approve each tool call. `git` is always allowed |
| `TAYLMAIL_MODEL` | CLI default | |

The working directory is fixed when a session starts, and later messages
keep using it.

## Debugging

```nu
# tests
.venv/bin/python -m unittest discover tests
# queued messages, outgoing mail and events for the chat
ls ~/.local/share/taylmail/inbox ~/.local/share/taylmail/outbox ~/.local/share/taylmail/events
```

## Known limits (prototype)

- Nobody is there to answer permission prompts, so any tool call the `auto`
  classifier denies stays denied. The denial is listed in the agent's reply.
- If you stop a chat turn in Open WebUI, the turn still runs. Its reply
  shows up in the chat as a new message when it's done.
- If the daemon dies after a turn but before clearing the turn's messages,
  that turn runs again.
- Only the `text/plain` part of your mail is passed to the agent.
  Attachments are ignored.
