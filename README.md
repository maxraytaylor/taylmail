# Taylmail

## === HUMAN WRITING ===

Development: github.com/maxraytaylor/taylmail

Greetings. I'm Max Raymundo Taylor and this is Taylmail, some prototypical software meant to help you review a coding agent's code more like you would with a *mailing list contributor* (receiving, applying, and replying to patch series emails) than you would with a *forge contributor* (reviewing and commenting on pull requests) or *pair programmer* (working in a shared directory and chatting).

Compared to those other common methods, this brings a different, well-established, text-editor-centric workflow to the process of reviewing AI agent code. If you're wondering about how email-based and pull-request-based code review compare, I'd highly recommend [Andrew Tropin's excellent YouTube video on the topic](https://www.youtube.com/live/XVe9SD3kSR0?si=_2I1vMFRdL59C3snj).

Taylmail also has a few technical advantages over the common alternative methods:

- Unlike working in a shared directory, this prevents malware from modifying CI configuration, build files, IDE configuration or Git hooks in the shared directory that your tools then access, such that they run arbitrary code in it when you perform an action as innoccuous as opening the project in an IDE or committing. `git diff` wouldn't reveal such modifications because they could be buried in a rewritten history, or made to a Git hook in `.git/` that `git diff` simply ignores, or made between the time of you reviewing the code and the time of you running it. Even the paranoid instructions in Docker Sandbox's documentation for its [direct mount mode](https://docs.docker.com/ai/sandboxes/security/isolation/#direct-mount), which advise the user to list the git hooks "after any agent session", wouldn't address these issues. Email patches, by contrast, can't rewrite your history or Git hooks, nor can they change the code from underneath you between the time of your review and the time of your execution, letting you be sure that the changes that you read are the only changes that you're running.

- Unlike any method of forge-style code review that I am aware of (e.g. the Claude Code GitHub action) this email method re-uses the agent's session cache, which should allow it to get back to work after receiving a comment more quickly and cheaply, and to answer questions about why it did something one way or another more accurately.

- Unlike `git fetch`'ing from a Git server in the agent's sandbox (as in Docker Sandbox's [clone mode](https://docs.docker.com/ai/sandboxes/security/isolation/#clone-mode), ostensibly their answer to direct mount's risks) and reviewing the agent's patches using plain `git diff` or `log`, this lets us re-use the features that TUI email clients have already refined over the years to facilitate code review. For instance, the common `r-q` keyboard shortcut that automatically block-quotes the whole email (e.g. patch email) and opens it in `$EDITOR` so that you can easily insert your comments under parts (e.g. lines of code) that you want to comment on, as well as delete the parts that don't need to be part of your reply. This lets you navigate through and comment on patch lines with the same ease and efficiency your text editor of choice affords you, in the way that has powered OSS collaboration on huge projects for decades.

Overall, I'm interested in this approach as a potential means to being able to securely and efficiently review agent code.

That said, all in all, is this a good idea? I'm not yet sure. Communicating with an agent via email introduces some of the complexities of email into the development process, such as the need to provide an email server between yourself and the agent, and to configure your email client to use that server. Some of this complexity might just be part of the nature of network communication -- one might otherwise have to run a Git server and configure one's Git client to use that server as a remote anyway -- but overall, I'm not familiar enough with email yet to know whether this is the right way to do things.

This vibe-coded prototype should help us all find out though. Yes I appreciate the irony of a vibe-coded code review tool. I'm aiming to use the slop to deal with the slop here. The vibe-coding process has, much faster than I could have done manually, gotten it into a state where one can run Taylmail as a server, plug Open WebUI into it, run an email server alongside, and ultimately achieve the goal of communicating with a single agent via both email and chat. There are still plenty of rough edges, but I'll test drive this prototypical form and find out if it's worth anyone's while. If it is, I'll develop a non-vibe-coded reimplementation for further development upon.

Otherwise, it may be best to keep `git fetch`'ing, `diff`'ing, and clicking and dragging to copy-paste the parts we want to comment on into the agent's chat window. Like a barbarian.

Frankly, while it should be possible in principle, I'm not yet even confident enough in its workings that I'd exactly "recommend" anyone else actually try installing this onto their machine.Claude even complains to me that there's a security issue with the current version (though one that seems, to me, implausible to exploit in the case where Taylmail runs in a VM on your local machine). But if you are really interested in this workflow as I am, you *could* ask your AI agent of choice to set this up in a VM for you so that you can try chatting, emailing, and see if the mailing-list style code collaboration workflow for agents sparks joy for you. Should be a lot easier than starting from scratch, at least. And if you do that (or don't do that) and thus come up with any feedback or insights to share, I'd of course be very grateful to receive that.

Otherwise, you could just let me continue test-driving it and check in now and again on progress. If it turns out to be worthwhile, I'll refine this into something practical. And if it turns out to be a terrible idea, I'll report that here to let the world know. You'll know where to find the updates either way.

Thus ends my explanation. I'll let the AI explain the details of exactly how it all works and how to get it working further below.

## === AI WRITING ===

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
nix run github:maxraytaylor/taylmail -- serve
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

| Variable                      | Default                     |                                                                                               |
| ----------------------------- | --------------------------- | --------------------------------------------------------------------------------------------- |
| `TAYLMAIL_USERS`              | (required)                  | your address(es); the only senders read and recipients mailed                                 |
| `TAYLMAIL_CWD`                | current directory           | where the agent works; a git repo                                                             |
| `TAYLMAIL_ADDRESS`            | `claude@taylmail.localhost` | the agent's address; sessions use `local+SESSION@domain`                                      |
| `TAYLMAIL_HOME`               | `~/.local/share/taylmail`   | state and spools                                                                              |
| `TAYLMAIL_IMAP`               | (required)                  | `imaps://host[:port]`, `imap://` (STARTTLS) or `imap+insecure://`                             |
| `TAYLMAIL_SMTP`               | (required)                  | `smtps://`, `smtp://` (STARTTLS) or `smtp+insecure://`                                        |
| `TAYLMAIL_MAIL_USER`          | `TAYLMAIL_ADDRESS`          | IMAP/SMTP login                                                                               |
| `TAYLMAIL_MAIL_PASSWORD_FILE` |                             | IMAP/SMTP password                                                                            |
| `TAYLMAIL_POLL_SECONDS`       | `15`                        | IMAP poll interval, only for servers without IDLE or Python before 3.14                       |
| `TAYLMAIL_SENDER_CHECK`       | `dmarc`                     | how to trust your From line; see [Mail server](#mail-server)                                  |
| `TAYLMAIL_CHAT_LISTEN`        | `127.0.0.1:8000`            | where the [chat API](#chat-api) listens                                                       |
| `TAYLMAIL_CHAT_API_KEY_FILE`  | (chat off)                  | bearer key adapters present                                                                   |
| `TAYLMAIL_CLAUDE`             | `claude` on `PATH`          | CLI the SDK drives (the pip-bundled one doesn't run on NixOS)                                 |
| `TAYLMAIL_PERMISSION_MODE`    | `auto`                      | SDK permission mode; `auto` lets a classifier approve each tool call. `git` is always allowed |
| `TAYLMAIL_MODEL`              | CLI default                 |                                                                                               |

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
