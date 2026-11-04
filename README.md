# taylmail

Talk to a Claude Code agent the way you'd talk to a contributor on a mailing
list. The agent sends patch series and you reply inline in aerc. You can also
chat with the same agent session, e.g. in Open WebUI, where each chat is a
session.

One daemon, `taylmail serve`, owns every session. Adapters queue your
messages, and the daemon runs one agent turn per session at a time:

```
aerc ──SMTP──▶ mail server ──IMAP──▶ ┐
chat frontend ─adapter─▶ POST /turn ┴─▶ inbox spool ─▶ taylmail serve ─▶ agent turn
                                                                              │
aerc ◀──IMAP── mail server ◀──SMTP── outbox ◀── a2u ◀── git send-email ◀──────┘
chat frontend ◀── /turn stream: status, "📧 Sent" notices, final reply
              ◀── adapter ◀── GET /events: mail turns, replies nobody waited for
```

- **Sessions.** The agent writes as `Claude <claude+SESSION@domain>`, so
  hitting Reply goes back to that session. If the plus-address gets lost,
  the `In-Reply-To`/`References` headers are looked up in a Message-ID map.
  Mail to plain `claude@domain` starts a new session. In a chat frontend, a
  new conversation starts a session and messages in it continue it.
- **Every session has a chat.** Mail that starts a session also starts a
  conversation in the chat frontend, titled with its subject. Your mails and the agent's
  replies show up there, so you can answer either way.
- **Patches always go by email.** When the agent sends mail, the session's
  chat is told what was sent.
- **Replies go back where the turn came from, and to the chat.** Messages
  that arrive while a turn runs are batched into the next turn. If the turn
  came by email and the agent sent no mail, its final message is mailed back
  as a reply. While a chat turn runs, its status line shows the agent's
  latest tool call.
- **No polling.** The daemon waits on IMAP IDLE, so it sees your mail as
  soon as the server has it (this needs Python 3.14). Adapters long-poll
  for mail turns, and `a2u` wakes the daemon over a Unix socket.
- **Durability.** Adapters write each message into a spool before
  acknowledging it. IMAP marks a mail seen only after queuing it, and an
  adapter's request is answered only after the message is queued. What the
  chat should show waits in an event spool until an adapter has written it.

## Install

With Nix, the flake's package brings Claude Code and git send-email along:

```nu
nix run github:OWNER/taylmail -- serve
```

In a NixOS configuration, add the flake as an input and use
`inputs.taylmail.packages.${system}.default`, or `pkgs.taylmail` with
`inputs.taylmail.overlays.default`. Claude Code is unfree, so the overlay
needs it allowed.

Without Nix:

```nu
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

## Chat

taylmail doesn't run a chat UI. A chat frontend reaches it through an
adapter that uses the chat API below. This repo has one adapter:

- [`openwebui/`](openwebui/README.md): an Open WebUI pipe function

Set `TAYLMAIL_CHAT_API_KEY_FILE` to turn the API on.

### Chat API

It listens on `TAYLMAIL_CHAT_LISTEN`. Every request needs
`Authorization: Bearer KEY`, with the contents of
`TAYLMAIL_CHAT_API_KEY_FILE`. An adapter names itself with a `client`
(lowercase letters, digits and dashes, e.g. `openwebui`). It names its
conversations with any string that is stable and unique within that client.
Each conversation is one session.

- `POST /turn` with `{"client", "conversation", "user", "text"}` queues
  `text` from `user` (a display name) for the conversation's session. A new
  conversation starts a session. The response streams one JSON object per
  line until the turn ends:
  - `{"status": "..."}`: the agent's latest tool call
  - `{"content": "..."}`: text to show before the reply, like "📧 Sent" notices
  - `{"reply": "..."}`: the final message; the stream ends here

  If the stream breaks, the turn runs on, and its reply becomes an event.
- `GET /events?client=CLIENT&wait=SECONDS` returns
  `{"events": [{"id", "session", "conversation", "title", "user", "reply"}]}`,
  oldest first. It waits up to `wait` seconds (at most 300) for one to
  arrive. Show `user` (if not null) as the user's message, then `reply` as
  the agent's. `conversation` is null if the session has none in this
  client yet. Then start one, titled `title`, and reuse it for later events
  of the same `session`.
- `POST /events/ID/ack` with `{"client", "conversation"}` drops the event
  once it is shown, and ties the session to `conversation`. Until then, the
  event stays and is returned again.

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
| `TAYLMAIL_CHAT_LISTEN` | `127.0.0.1:8000` | where the [chat API](#chat-api) listens |
| `TAYLMAIL_CHAT_API_KEY_FILE` | (chat off) | bearer key adapters present |
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
- If the daemon dies after a turn but before clearing the turn's messages,
  that turn runs again.
- Only the `text/plain` part of your mail is passed to the agent.
  Attachments are ignored.
