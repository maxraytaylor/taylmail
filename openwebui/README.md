# taylmail for Open WebUI

An Open WebUI [pipe function][pipes] that lets you chat with
[taylmail](../README.md) sessions. It shows up as a model named after the
function, and each chat with it is one session. Because it runs inside Open
WebUI, it also writes mail turns into your chats. Mail that starts a session
starts a chat, titled with its subject.

It talks to taylmail's [chat API](../README.md#chat-api) as the client
`openwebui`. It was written against Open WebUI 0.12.5 and uses Open WebUI's
own chat and user models, so other versions may need changes.

## Install

1. Run `taylmail serve` with `TAYLMAIL_CHAT_API_KEY_FILE` set, and note the
   key. One way to make it:

   ```nu
   python3 -c 'import secrets; print(secrets.token_urlsafe(32))' | save ~/.config/taylmail/chat_api_key
   ```

2. As an admin, go to Admin Panel → Functions, add a function and paste
   [`taylmail_pipe.py`](taylmail_pipe.py) into it. Call it `Claude`, for
   example.
3. In the function's valves, set `TAYLMAIL_KEY` to the key, and
   `TAYLMAIL_URL` if taylmail doesn't listen on `http://127.0.0.1:8000`.
   Then enable the function.
4. Start a new chat with the `Claude` model.

## Limits

- Only one Open WebUI user talks to the agent and gets the chats that mail
  starts: the `OWNER_EMAIL` valve, or by default the first admin.
- Temporary chats don't work, since they have no ID to tie a session to.
- The pipe collects mail turns while Open WebUI runs and its model list has
  been loaded, e.g. by opening Open WebUI once. Until then they wait in
  taylmail.
- If you stop a turn, it still runs. Its reply shows up in the chat as a
  new message when it's done.

[pipes]: https://docs.openwebui.com/features/extensibility/plugin/functions/pipe
