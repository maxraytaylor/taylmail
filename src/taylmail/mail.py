"""Email adapter: user mail in (u2a spool or IMAP), agent mail out (Maildir or SMTP)."""

import email
import email.policy
import imaplib
import json
import logging
import mailbox
import re
import smtplib
import ssl
import time
import uuid
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

from . import inbox
from .config import Config, Server
from .store import Store

log = logging.getLogger("taylmail.mail")


def parse(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.default)


def session_from_addresses(cfg: Config, *headers: str | None) -> str | None:
    pattern = re.compile(rf"^{re.escape(cfg.local)}\+([0-9a-f]{{8}}(?:-[0-9a-f]{{4}}){{3}}-[0-9a-f]{{12}})@", re.IGNORECASE)
    for _, addr in getaddresses([h for h in headers if h]):
        if m := pattern.match(addr):
            return m.group(1).lower()
    return None


def find_session(cfg: Config, msg: EmailMessage, recipients: list[str]) -> str | None:
    """Plus-address first, then the Message-ID map via In-Reply-To/References."""
    to_headers = msg.get_all("To", []) + msg.get_all("Cc", []) + msg.get_all("Delivered-To", [])
    if sid := session_from_addresses(cfg, ", ".join(recipients), *to_headers):
        return sid
    refs = (msg["References"] or "").split() + (msg["In-Reply-To"] or "").split()
    return Store(cfg.home).lookup(refs)


def sender(msg: EmailMessage) -> str:
    return getaddresses([msg["From"] or ""])[0][1].lower()


def authenticated(msg: EmailMessage) -> bool:
    """Did the receiving server vouch for the From domain?

    Only the topmost Authentication-Results header counts: the receiving
    server adds it last, and anything below it could come from the sender.
    """
    results = msg.get_all("Authentication-Results") or []
    if not results:
        return False
    top = str(results[0]).lower()
    domain = sender(msg).rpartition("@")[2]
    if re.search(rf"\bdmarc=pass\b[^;]*header\.from={re.escape(domain)}\b", top):
        return True
    return bool(re.search(rf"\bdkim=pass\b[^;]*header\.[di]=@?{re.escape(domain)}\b", top))


def authorized(cfg: Config, msg: EmailMessage, *, trusted: bool) -> str | None:
    """Reason to drop this mail, or None if the agent may read it."""
    if cfg.users and sender(msg) not in {u.lower() for u in cfg.users}:
        return f"sender {sender(msg)} not in TAYLMAIL_USERS"
    if not (trusted or cfg.trust_from or authenticated(msg)):
        return "no passing DMARC/DKIM in Authentication-Results"
    return None


def render_prompt(msg: EmailMessage) -> str:
    body_part = msg.get_body(preferencelist=("plain",))
    body = body_part.get_content() if body_part else "(no text/plain body)"
    head = "\n".join(
        f"{name}: {msg[name]}"
        for name in ("From", "Date", "Subject", "Message-ID", "In-Reply-To")
        if msg[name]
    )
    return f"{head}\n\n{body}"


def intake(cfg: Config, raw: bytes, recipients: list[str] = (), *, trusted: bool) -> str | None:
    """Queue a user mail for its session. Returns the session ID, or None if dropped."""
    msg = parse(raw)
    if reason := authorized(cfg, msg, trusted=trusted):
        log.warning("dropping %s: %s", msg["Message-ID"], reason)
        return None
    store = Store(cfg.home)
    session_id = find_session(cfg, msg, list(recipients))
    if session_id is None or store.session(session_id) is None:
        # Don't trust a session ID the sender made up.
        session_id = str(uuid.uuid4())
        store.update_session(session_id, started=False, cwd=str(cfg.cwd))
    if not store.session(session_id).get("user"):
        store.update_session(session_id, user=sender(msg))
    mid = (msg["Message-ID"] or "").strip() or None
    if mid:
        store.record_message(mid, session_id, sent=False)
        store.update_session(session_id, last_email=mid)
    ctx = {
        "message_id": mid,
        "subject": msg["Subject"] or "",
        "references": msg["References"] or "",
        "reply_to": msg["Reply-To"] or msg["From"],
    }
    inbox.put(cfg, session_id, "email", render_prompt(msg), email=ctx)
    return session_id


def reply_to(cfg: Config, ctx: dict, session_id: str, body: str) -> EmailMessage:
    """A plain reply to the user mail described by a job's `email` context."""
    reply = EmailMessage()
    subject = ctx.get("subject", "")
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    reply["From"] = cfg.agent_from(session_id)
    reply["To"] = ctx["reply_to"]
    if mid := ctx.get("message_id"):
        reply["In-Reply-To"] = mid
        reply["References"] = " ".join(ctx.get("references", "").split() + [mid])
    reply.set_content(body)
    return reply


# --- Outgoing ----------------------------------------------------------------


def queue_outgoing(cfg: Config, raw: bytes, recipients: list[str], session_id: str | None) -> None:
    """a2u's half: park the agent's mail for the daemon to send."""
    d = cfg.home / "outbox"
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{time.time_ns()}"
    (d / f"{stem}.eml").write_bytes(raw)
    meta = {"recipients": recipients, "session": session_id}
    tmp = d / f"{stem}.tmp"
    tmp.write_text(json.dumps(meta))
    tmp.replace(d / f"{stem}.json")  # the .json appearing marks the pair complete


def drain_outbox(cfg: Config) -> list[tuple[str | None, EmailMessage]]:
    """Send everything a2u queued. Returns (session, message) for each one sent."""
    d = cfg.home / "outbox"
    sent = []
    for meta_path in sorted(d.glob("*.json")) if d.is_dir() else []:
        eml = meta_path.with_suffix(".eml")
        meta = json.loads(meta_path.read_text())
        msg = parse(eml.read_bytes())
        send(cfg, msg, session_id=meta["session"], recipients=meta["recipients"])
        sent.append((meta["session"], msg))
        eml.unlink()
        meta_path.unlink()
    return sent


def send(
    cfg: Config, msg: EmailMessage, *, session_id: str | None, recipients: list[str] = ()
) -> str:
    """Finish the headers, remember the Message-ID, and deliver. Returns the Message-ID."""
    session_id = session_id or session_from_addresses(cfg, msg["From"])
    if session_id:
        # A plain Reply must come back to this session.
        del msg["From"], msg["Reply-To"]
        msg["From"] = cfg.agent_from(session_id)
        msg["Reply-To"] = cfg.agent_address(session_id)
    if not msg["Message-ID"]:
        msg["Message-ID"] = make_msgid(domain=cfg.domain)
    if not msg["Date"]:
        msg["Date"] = formatdate(localtime=True)
    mid = msg["Message-ID"].strip()
    if session_id:
        store = Store(cfg.home)
        store.record_message(mid, session_id, sent=True)
        store.update_session(session_id, last_email=mid)

    if cfg.smtp:
        _smtp_send(cfg, msg, _envelope(cfg, msg, recipients))
    else:
        maildir_add(cfg, msg)
    return mid


def _envelope(cfg: Config, msg: EmailMessage, recipients: list[str]) -> list[str]:
    """Only ever mail the configured users, whatever the agent asked for."""
    allowed = {u.lower() for u in cfg.users}
    wanted = [a for _, a in getaddresses(list(recipients) + msg.get_all("To", []))]
    rcpts = sorted({a.lower() for a in wanted if a.lower() in allowed})
    if dropped := {a.lower() for a in wanted} - allowed:
        log.warning("not mailing %s: not in TAYLMAIL_USERS", ", ".join(sorted(dropped)))
    return rcpts or [cfg.user]


def maildir_add(cfg: Config, msg: EmailMessage, *, seen: bool = False) -> None:
    cfg.maildir.parent.mkdir(parents=True, exist_ok=True)
    box = mailbox.Maildir(cfg.maildir, create=True)
    mmsg = mailbox.MaildirMessage(msg.as_bytes())
    if seen:
        mmsg.set_subdir("cur")
        mmsg.add_flag("S")
    box.add(mmsg)


# --- Remote servers ----------------------------------------------------------


def _password(cfg: Config) -> str:
    if not cfg.mail_password_file:
        raise RuntimeError("set TAYLMAIL_MAIL_PASSWORD_FILE")
    return cfg.mail_password_file.read_text().strip()


def _login_user(cfg: Config) -> str:
    return cfg.mail_user or cfg.address


def _smtp_send(cfg: Config, msg: EmailMessage, rcpts: list[str]) -> None:
    s: Server = cfg.smtp
    ctx = ssl.create_default_context()
    cls = smtplib.SMTP_SSL if s.tls == "implicit" else smtplib.SMTP
    kwargs = {"context": ctx} if s.tls == "implicit" else {}
    with cls(s.host, s.port, timeout=60, **kwargs) as smtp:
        if s.tls == "starttls":
            smtp.starttls(context=ctx)
        smtp.ehlo()
        if smtp.has_extn("auth"):
            smtp.login(_login_user(cfg), _password(cfg))
        smtp.send_message(msg, from_addr=cfg.address, to_addrs=rcpts)


def imap_fetch(cfg: Config, handle) -> int:
    """Pass each unseen mail in the agent's INBOX to `handle`, then mark it seen."""
    s: Server = cfg.imap
    ctx = ssl.create_default_context()
    if s.tls == "implicit":
        imap = imaplib.IMAP4_SSL(s.host, s.port, ssl_context=ctx, timeout=60)
    else:
        imap = imaplib.IMAP4(s.host, s.port, timeout=60)
        if s.tls == "starttls":
            imap.starttls(ssl_context=ctx)
    try:
        imap.login(_login_user(cfg), _password(cfg))
        imap.select("INBOX")
        _, data = imap.uid("SEARCH", None, "UNSEEN")
        uids = data[0].split()
        for uid in uids:
            _, parts = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            handle(next(p[1] for p in parts if isinstance(p, tuple)))
            imap.uid("STORE", uid, "+FLAGS", r"(\Seen)")
        return len(uids)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
