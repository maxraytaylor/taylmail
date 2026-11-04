# taylmail

Talk to a Claude Code agent the way you'd talk to a contributor on a mailing
list. The agent sends patch series and you reply inline in aerc. You can also
chat with the same agent session in an Open WebUI channel thread.

One daemon, `taylmail serve`, owns every session. Adapters queue your
messages, and the daemon runs one agent turn per session at a time:

```
aerc ──SMTP──▶ mail server ──IMAP──▶ ┐
Open WebUI channel ──Socket.IO──────▶ ┴─▶ inbox spool ─▶ taylmail serve ─▶ agent turn
                                                                              │
aerc ◀──IMAP── mail server ◀──SMTP── outbox ◀── a2u ◀── git send-email ◀──────┘
Open WebUI thread ◀── status message, final reply, "📧 Sent" notices
```

- **Sessions.** The agent writes as `Claude <claude+SESSION@domain>`, so
  hitting Reply goes back to that session. If the plus-address gets lost,
  the `In-Reply-To`/`References` headers are looked up in a Message-ID map.
  Mail to plain `claude@domain` starts a new session. In chat, a top-level
  channel message starts a session and the agent answers in its thread.
  Messages in that thread continue the same session.
- **Patches always go by email.** When the agent sends mail, the session's
  chat thread is told what was sent.
- **Replies go back where the turn came from.** Messages that arrive while a
  turn runs are batched into the next turn. If the turn came by email and
  the agent sent no mail, its final message is mailed back as a reply. Chat
  messages get their answer in the thread. While a turn runs, a "⏳ Working…"
  message shows its recent tool calls. When the turn ends, that message
  becomes the reply.
- **No polling.** The daemon waits on IMAP IDLE, so it sees your mail as
  soon as the server has it (this needs Python 3.14). Open WebUI pushes chat
  messages, and `a2u` wakes the daemon over a Unix socket.
- **Durability.** Adapters write each message into a spool before
  acknowledging it. IMAP marks a mail seen only after queuing it, and
  Open WebUI gets its delivery receipt only after the message is queued.

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

Only mail from `TAYLMAIL_USERS` is read. The mail server must also vouch for
it: the topmost `Authentication-Results` header needs `dmarc=pass` or
`dkim=pass` for the sender's domain. The agent only ever mails
`TAYLMAIL_USERS`, whatever addresses it asks for.

## Chat: Open WebUI Channel App

taylmail serves an Open WebUI [Channel App][apps]. Open WebUI connects to
the app, so the daemon can stay on your private network.

1. Make the app's keys:

   ```nu
   .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))' | save ~/.config/taylmail/chat_api_key
   .venv/bin/python -c 'from cryptography.hazmat.primitives.asymmetric import ed25519; from cryptography.hazmat.primitives import serialization as s; print(ed25519.Ed25519PrivateKey.generate().private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()).decode())' | save ~/.config/taylmail/chat_key.pem
   ```

2. Point the daemon at them:

   ```nu
   $env.TAYLMAIL_CHAT_API_KEY_FILE = "~/.config/taylmail/chat_api_key"
   $env.TAYLMAIL_CHAT_PRIVATE_KEY_FILE = "~/.config/taylmail/chat_key.pem"
   ```

3. Start Open WebUI with `ENABLE_APPS=true` and `ENABLE_CHANNELS=true`.
   Open WebUI only accepts HTTPS app URLs. For plain HTTP on localhost or
   a LAN, also set `APP_ALLOWED_PRIVATE_ORIGINS=http://host:8000`.
4. As an admin, install the app with the URL `http://host:8000/events` and
   the API key. Grant both scopes it requests,
   `channels:messages:read` and `channels:messages:write`, and connect it.
5. Add the app to a channel and post a message there.

Apps can't join direct messages. A channel used only by you works the same
way. To restrict who can talk to the agent, set `TAYLMAIL_CHAT_USERS` to
Open WebUI user IDs.

[apps]: https://docs.openwebui.com/features/administration/webhooks/

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
| `TAYLMAIL_TRUST_FROM` | off | `1` skips the DMARC/DKIM check (testing only) |
| `TAYLMAIL_CHAT_LISTEN` | `127.0.0.1:8000` | where the Channel App listens |
| `TAYLMAIL_CHAT_API_KEY_FILE` | (chat off) | key Open WebUI presents when connecting |
| `TAYLMAIL_CHAT_PRIVATE_KEY_FILE` | | Ed25519 PEM the app signs with |
| `TAYLMAIL_CHAT_USERS` | anyone in the channel | comma-separated Open WebUI user IDs |
| `TAYLMAIL_CLAUDE` | `claude` on `PATH` | CLI the SDK drives (the pip-bundled one doesn't run on NixOS) |
| `TAYLMAIL_PERMISSION_MODE` | `auto` | SDK permission mode; `auto` lets a classifier approve each tool call. `git` is always allowed |
| `TAYLMAIL_MODEL` | CLI default | |

The working directory is fixed when a session starts, and later messages
keep using it.

## Debugging

```nu
# tests
.venv/bin/python -m unittest discover tests
# queued messages and outgoing mail
ls ~/.local/share/taylmail/inbox ~/.local/share/taylmail/outbox
```

## Known limits (prototype)

- Nobody is there to answer permission prompts, so any tool call the `auto`
  classifier denies stays denied. The denial is listed in the agent's reply.
- A session started by email has no chat thread. Its replies go by email
  only.
- If the daemon dies after a turn but before clearing the turn's messages,
  that turn runs again.
- Only the `text/plain` part of your mail is passed to the agent.
  Attachments are ignored.
