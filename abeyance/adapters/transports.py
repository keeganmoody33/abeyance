"""Asking, and hearing back.

All three transports enforce the same two rules, and both exist because getting them wrong is
survivable-looking and not survivable:

  **Attribute every reply to a sender.** Multi-approver consent is impossible without it, and
  the moment a loop addresses real recipients, "the reply" stops being a meaningful phrase.

  **Never mistake our own message for an answer.** Excluding by message id alone is not
  enough: a message we sent but failed to record — a crash between the send and the state
  write — has an id the state does not know, and comes back looking inbound. An apply tick
  then reads our own proposal text as somebody's approval. Every transport here therefore
  also filters by sender address.
"""
from __future__ import annotations

import base64
import email
import email.utils
import imaplib
import re
import smtplib
import time
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from ..errors import TransportError
from ..models import Reply, Sent

QUOTE_MARKERS = (
    re.compile(r"^>"),
    re.compile(r"^On .*wrote:\s*$"),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.I),
    re.compile(r"^_{5,}\s*$"),
    re.compile(r"^Sent from my ", re.I),
    re.compile(r"^From:\s.*@", re.I),
)


def strip_quoted(body: str, *, fallback_chars: int = 500) -> str:
    """Everything the person actually typed, with the quoted history cut off.

    Cheap and heuristic on purpose. It runs before a human or a model reads the reply, so a
    mistake here costs a slightly noisy excerpt, not a wrong decision — and the raw text is
    kept on the approver record either way.
    """
    out: List[str] = []
    for line in (body or "").splitlines():
        if any(m.match(line.strip()) for m in QUOTE_MARKERS):
            break
        out.append(line)
    text = "\n".join(out).strip()
    return text or (body or "").strip()[:fallback_chars]


def address_of(header_value: str) -> str:
    """Bare address out of `Name <a@b.c>`, lowercased. Casing in a From header is not under
    our control, and a case-sensitive approver lookup turns a real approval into silence."""
    _, addr = email.utils.parseaddr(header_value or "")
    return (addr or header_value or "").strip().strip("<>").lower()


# --------------------------------------------------------------------------- memory


@dataclass
class _MemMessage:
    message_id: str
    thread_id: str
    sender: str
    to: List[str]
    subject: str
    body: str
    epoch: int
    header_message_id: str


class MemoryTransport:
    """A transport you drive by hand. The examples and the whole test suite run on it.

    It exists so the interesting paths — a two-approver split, a reply from a stranger, a
    clarification round, an expiry — are testable in milliseconds with no mailbox, no
    credentials and no network. Those are the paths that break in production, so they are the
    ones that have to be cheap to exercise.
    """

    def __init__(self, address: str = "loop@example.test") -> None:
        self._address = address.lower()
        self.sent: List[_MemMessage] = []
        self.inbox: List[_MemMessage] = []
        self._n = 0

    @property
    def address(self) -> str:
        return self._address

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:04d}"

    def send(self, to: Sequence[str], subject: str, body: str, cc: Sequence[str] = (),
             thread_id: Optional[str] = None, in_reply_to: Optional[str] = None) -> Sent:
        mid = self._next("m")
        tid = thread_id or self._next("t")
        msg = _MemMessage(message_id=mid, thread_id=tid, sender=self._address,
                          to=[t.lower() for t in to], subject=subject, body=body,
                          epoch=int(time.time()), header_message_id=f"<{mid}@memory>")
        self.sent.append(msg)
        return Sent(message_id=mid, thread_id=tid, header_message_id=msg.header_message_id)

    def receive(self, thread_id: str, sender: str, text: str,
                epoch: Optional[int] = None) -> Reply:
        """Test hook: pretend somebody replied."""
        mid = self._next("r")
        msg = _MemMessage(message_id=mid, thread_id=thread_id, sender=sender.lower(),
                          to=[self._address], subject="Re:", body=text,
                          epoch=int(epoch if epoch is not None else time.time()),
                          header_message_id=f"<{mid}@memory>")
        self.inbox.append(msg)
        return Reply(message_id=mid, sender=msg.sender, text=text, epoch=msg.epoch)

    def fetch_replies(self, thread_id: str, exclude_ids: Iterable[str] = (),
                      after_epoch: int = 0) -> List[Reply]:
        skip = {str(i) for i in exclude_ids}
        return [Reply(message_id=m.message_id, sender=m.sender, text=m.body, epoch=m.epoch)
                for m in sorted(self.inbox, key=lambda x: (x.epoch, x.message_id))
                if m.thread_id == thread_id and m.message_id not in skip
                and m.epoch >= after_epoch and m.sender != self._address]

    def last_sent(self) -> Optional[_MemMessage]:
        return self.sent[-1] if self.sent else None


# --------------------------------------------------------------------------- gmail


class GmailTransport:
    """Gmail API. Requires the `gmail` extra and an authorized token.

    Gmail's `threadId` is the natural correlation key and becomes the proposal id, so an
    operator can paste the id into the Gmail UI and read the whole negotiation. That
    traceability is worth more than it sounds when you are reconstructing who approved what.
    """

    SCOPES = ("https://www.googleapis.com/auth/gmail.modify",)

    def __init__(self, service: Any = None, *, token_path: Optional[str] = None,
                 sender: Optional[str] = None) -> None:
        if service is None and not token_path:
            raise TransportError("GmailTransport needs either a built `service` or a "
                                 "`token_path` to an authorized token.json")
        self._service = service
        self._token_path = token_path
        self._address = (sender or "").lower()

    # -- auth ------------------------------------------------------------

    @property
    def service(self) -> Any:
        if self._service is None:
            self._service = self._build()
        return self._service

    def _build(self) -> Any:
        try:
            from google.auth.transport.requests import Request  # type: ignore
            from google.oauth2.credentials import Credentials  # type: ignore
            from googleapiclient.discovery import build  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                'GmailTransport needs the gmail extra: pip install "abeyance[gmail]"'
            ) from e
        from pathlib import Path
        p = Path(self._token_path)  # type: ignore[arg-type]
        creds = Credentials.from_authorized_user_file(str(p), list(self.SCOPES))
        if not creds.valid:
            creds.refresh(Request())
            p.write_text(creds.to_json())
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    @property
    def address(self) -> str:
        if not self._address:
            self._address = self.service.users().getProfile(
                userId="me").execute()["emailAddress"].lower()
        return self._address

    # -- send ------------------------------------------------------------

    def send(self, to: Sequence[str], subject: str, body: str, cc: Sequence[str] = (),
             thread_id: Optional[str] = None, in_reply_to: Optional[str] = None) -> Sent:
        msg = MIMEText(body, _charset="utf-8")
        msg["to"] = ", ".join(to)
        if cc:
            msg["cc"] = ", ".join(cc)
        msg["from"] = self.address
        msg["subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        payload: Dict[str, Any] = {
            "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if thread_id:
            payload["threadId"] = thread_id
        try:
            sent = self.service.users().messages().send(userId="me", body=payload).execute()
        except Exception as e:  # noqa: BLE001
            raise TransportError(f"gmail send failed: {e}") from e
        header_id = self._header(self._get(sent["id"]), "Message-Id")
        return Sent(message_id=sent["id"], thread_id=sent["threadId"],
                    header_message_id=header_id)

    # -- read ------------------------------------------------------------

    def fetch_replies(self, thread_id: str, exclude_ids: Iterable[str] = (),
                      after_epoch: int = 0) -> List[Reply]:
        skip = {str(i) for i in exclude_ids}
        me = self.address
        try:
            thread = self.service.users().threads().get(
                userId="me", id=thread_id, format="full").execute()
        except Exception as e:  # noqa: BLE001
            raise TransportError(f"gmail thread {thread_id} unreadable: {e}") from e

        out: List[Reply] = []
        for m in sorted(thread.get("messages", []), key=lambda x: int(x["internalDate"])):
            if m["id"] in skip:
                continue
            epoch = int(m["internalDate"]) // 1000
            if epoch < after_epoch:
                continue
            sender = address_of(self._header(m, "From"))
            if sender == me:
                continue
            out.append(Reply(message_id=m["id"], sender=sender, epoch=epoch,
                             text=strip_quoted(self._plain_text(m))))
        return out

    def _get(self, message_id: str) -> Dict[str, Any]:
        return self.service.users().messages().get(
            userId="me", id=message_id, format="metadata",
            metadataHeaders=["Message-Id"]).execute()

    @staticmethod
    def _header(msg: Dict[str, Any], name: str) -> str:
        for h in msg.get("payload", {}).get("headers", []) or []:
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
        return ""

    @staticmethod
    def _plain_text(msg: Dict[str, Any]) -> str:
        def walk(part: Dict[str, Any]) -> str:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(
                    part["body"]["data"]).decode("utf-8", "replace")
            for sub in part.get("parts") or []:
                found = walk(sub)
                if found:
                    return found
            return ""
        return walk(msg.get("payload", {}) or {})


# --------------------------------------------------------------------------- smtp + imap


class SMTPIMAPTransport:
    """Send over SMTP, read over IMAP. Stdlib only — no vendor SDK, no OAuth app to register.

    Threading is by RFC-5322 `References`/`In-Reply-To`, and the proposal id is the original
    `Message-ID`. That is weaker than a provider thread id: a client that strips the headers
    breaks the correlation. In exchange it works against any mailbox, which for a
    self-hosted or on-prem deployment is the difference between usable and not.
    """

    def __init__(self, *, address: str, smtp_host: str, smtp_port: int = 587,
                 imap_host: str = "", imap_port: int = 993,
                 username: str = "", password: str = "", mailbox: str = "INBOX",
                 use_tls: bool = True) -> None:
        self.address = address.lower()  # type: ignore[assignment]
        self.smtp_host, self.smtp_port = smtp_host, smtp_port
        self.imap_host, self.imap_port = imap_host or smtp_host, imap_port
        self.username = username or address
        self.password = password
        self.mailbox = mailbox
        self.use_tls = use_tls

    def send(self, to: Sequence[str], subject: str, body: str, cc: Sequence[str] = (),
             thread_id: Optional[str] = None, in_reply_to: Optional[str] = None) -> Sent:
        msg = MIMEText(body, _charset="utf-8")
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["From"] = self.address
        msg["Subject"] = subject
        mid = email.utils.make_msgid(domain=self.address.split("@")[-1])
        msg["Message-ID"] = mid
        anchor = in_reply_to or thread_id
        if anchor:
            msg["In-Reply-To"] = anchor
            msg["References"] = anchor
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as s:
                if self.use_tls:
                    s.starttls()
                if self.password:
                    s.login(self.username, self.password)
                s.send_message(msg, to_addrs=list(to) + list(cc))
        except Exception as e:  # noqa: BLE001
            raise TransportError(f"smtp send failed: {e}") from e
        return Sent(message_id=mid, thread_id=thread_id or mid, header_message_id=mid)

    def fetch_replies(self, thread_id: str, exclude_ids: Iterable[str] = (),
                      after_epoch: int = 0) -> List[Reply]:
        skip = {str(i) for i in exclude_ids}
        out: List[Reply] = []
        try:
            with imaplib.IMAP4_SSL(self.imap_host, self.imap_port) as im:
                im.login(self.username, self.password)
                im.select(self.mailbox)
                # Match on References OR In-Reply-To: clients disagree about which they set on
                # a reply, and matching only one loses whole conversations from whole clients.
                seen: Dict[str, Any] = {}
                for header in ("References", "In-Reply-To"):
                    typ, data = im.search(None, "HEADER", header, thread_id)
                    if typ != "OK":
                        continue
                    for num in (data[0] or b"").split():
                        typ, raw = im.fetch(num, "(RFC822)")
                        if typ != "OK" or not raw or not raw[0]:
                            continue
                        m = email.message_from_bytes(raw[0][1])
                        seen[m.get("Message-ID", num.decode())] = m
                for mid, m in seen.items():
                    if mid in skip:
                        continue
                    sender = address_of(m.get("From", ""))
                    if sender == self.address:
                        continue
                    epoch = int(email.utils.mktime_tz(
                        email.utils.parsedate_tz(m.get("Date")) or (0,) * 10)) or 0
                    if epoch and epoch < after_epoch:
                        continue
                    out.append(Reply(message_id=mid, sender=sender, epoch=epoch,
                                     text=strip_quoted(_imap_body(m))))
        except TransportError:
            raise
        except Exception as e:  # noqa: BLE001
            raise TransportError(f"imap read failed: {e}") from e
        return sorted(out, key=lambda r: (r.epoch, r.message_id))


def _imap_body(msg: Any) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", "replace")


# --------------------------------------------------------------------------- wrapper


@dataclass
class CallableTransport:
    """Adapt an existing mail helper without writing a class.

    Most teams adding this to a live system already have a `send_mail()` somewhere with the
    auth solved. This lets that function be the transport, which keeps the migration a
    one-file change instead of a credentials project.
    """

    address: str
    send_fn: Callable[..., Sent]
    fetch_fn: Callable[..., List[Reply]]
    _: Any = field(default=None, repr=False)

    def send(self, to: Sequence[str], subject: str, body: str, cc: Sequence[str] = (),
             thread_id: Optional[str] = None, in_reply_to: Optional[str] = None) -> Sent:
        return self.send_fn(to=to, subject=subject, body=body, cc=cc,
                            thread_id=thread_id, in_reply_to=in_reply_to)

    def fetch_replies(self, thread_id: str, exclude_ids: Iterable[str] = (),
                      after_epoch: int = 0) -> List[Reply]:
        return self.fetch_fn(thread_id=thread_id, exclude_ids=exclude_ids,
                             after_epoch=after_epoch)
