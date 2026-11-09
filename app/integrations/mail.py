"""iCloud Mail přes IMAP – POUZE ČTENÍ.

Bezpečnost:
- schránky se otevírají jen pro čtení (EXAMINE = select(readonly=True)),
- zprávy se stahují přes BODY.PEEK – neoznačí se jako přečtené, nic se nemění,
- žádné odesílání, mazání ani přesouvání (SMTP tu vůbec není),
- obsah e-mailů je nedůvěryhodný vstup (prompt injection) – zpracování viz app/tools/mail.py.
"""

import email
import email.policy
import imaplib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Protocol

BODY_LIMIT = 4000  # znaků těla zprávy předaných modelu
SNIPPET_LIMIT = 300  # náhled pro předtřídění
SNIPPET_MAX_SIZE = 400_000  # větší zprávy (velké přílohy) se pro náhled nestahují
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES"
FOLDER_CODES = {"I": "INBOX", "S": "Sent Messages"}  # iCloud název odeslané pošty


class MailError(Exception):
    """Chyba spojení nebo čtení pošty (vrací se modelu jako chyba nástroje)."""


@dataclass(frozen=True)
class Address:
    name: str
    email: str

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>" if self.name else self.email


@dataclass
class MailHeader:
    ref: str  # E-I1234 (složka + UID)
    folder: str
    uid: int
    date: datetime | None
    sender: Address | None
    to: list[Address]
    cc: list[Address]
    subject: str
    message_id: str
    in_reply_to: str
    references: list[str]
    seen: bool = True
    size: int = 0


@dataclass
class Attachment:
    filename: str
    content_type: str
    size: int


@dataclass
class MailMessage:
    header: MailHeader
    body: str
    truncated: bool
    attachments: list[Attachment] = field(default_factory=list)


# ---------------------------------------------------------------- parsování


class _TextFromHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextFromHtml()
    p.feed(html)
    return normalize_ws("".join(p.parts))


def normalize_ws(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def strip_quoted(text: str) -> str:
    """Odstraní citovanou předchozí korespondenci (řádky „>“ a vše od „Dne … napsal:“)."""
    lines = []
    for line in text.split("\n"):
        if re.match(r"^\s*(Dne |On ).{5,200}(napsal|napsala|wrote)\s*:?\s*$", line):
            break
        if re.match(r"^-{2,}\s*(Original Message|Původní zpráva|Forwarded)", line, re.I):
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _addresses(value: Any) -> list[Address]:
    if not value:
        return []
    return [Address(n.strip(), e.strip().lower()) for n, e in getaddresses([str(value)]) if e]


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        d = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _ids(value: Any) -> list[str]:
    return re.findall(r"<[^<>\s]+>", str(value or ""))


def parse_header(
    raw: bytes, folder_code: str, uid: int, *, seen: bool = True, size: int = 0
) -> MailHeader:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    senders = _addresses(msg.get("From"))
    return MailHeader(
        ref=f"E-{folder_code}{uid}",
        folder=FOLDER_CODES[folder_code],
        uid=uid,
        date=_date(msg.get("Date")),
        sender=senders[0] if senders else None,
        to=_addresses(msg.get("To")),
        cc=_addresses(msg.get("Cc")),
        subject=str(msg.get("Subject") or "(bez předmětu)").strip(),
        message_id=(_ids(msg.get("Message-ID")) or [""])[0],
        in_reply_to=(_ids(msg.get("In-Reply-To")) or [""])[0],
        references=_ids(msg.get("References")),
        seen=seen,
        size=size,
    )


def parse_message(raw: bytes, folder_code: str, uid: int, *, seen: bool = True) -> MailMessage:
    header = parse_header(raw, folder_code, uid, seen=seen, size=len(raw))
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    attachments: list[Attachment] = []
    plain: list[str] = []
    html: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        ctype = part.get_content_type()
        if disposition == "attachment" or (filename and disposition != "inline"):
            payload = part.get_payload(decode=True) or b""
            attachments.append(Attachment(filename or "(bez názvu)", ctype, len(payload)))
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            continue
        if ctype == "text/plain" and isinstance(content, str):
            plain.append(content)
        elif ctype == "text/html" and isinstance(content, str):
            html.append(content)
    text = normalize_ws("\n\n".join(plain)) if plain else html_to_text("\n".join(html))
    text = strip_quoted(text) or text
    truncated = len(text) > BODY_LIMIT
    return MailMessage(header, text[:BODY_LIMIT], truncated, attachments)


def normalize_subject(subject: str) -> str:
    s = subject.lower()
    s = re.sub(r"^\s*((re|fw|fwd|odp|vs|aw|tr)\s*(\[\d+\])?\s*:\s*)+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def imap_since(dt: datetime) -> str:
    """IMAP datum (SINCE má přesnost na dny, anglické zkratky měsíců nezávisle na locale)."""
    return f"{dt.day:02d}-{MONTHS[dt.month - 1]}-{dt.year}"


# ------------------------------------------------------------------ backend


class MailBackend(Protocol):
    async def headers_since(
        self, folder_code: str, since: datetime, limit: int
    ) -> list[MailHeader]: ...
    async def message(self, folder_code: str, uid: int) -> MailMessage: ...


def snippet_of(raw: bytes, folder_code: str, uid: int) -> str:
    try:
        body = parse_message(raw, folder_code, uid).body
    except Exception:  # noqa: BLE001 – poškozená zpráva nesmí shodit třídění
        return ""
    return re.sub(r"\s+", " ", body)[:SNIPPET_LIMIT]


class ImapMailBackend:
    """Skutečný iCloud IMAP. Každé volání = krátké spojení (žádná trvale otevřená session)."""

    def __init__(
        self,
        user: str,
        password: str,
        host: str = "imap.mail.me.com",
        port: int = 993,
        timeout: float = 30.0,
        imap_factory: Any = None,
    ):
        self._user, self._password = user, password
        self._host, self._port, self._timeout = host, port, timeout
        self._factory = imap_factory or imaplib.IMAP4_SSL

    def _connect(self, folder: str) -> Any:
        try:
            conn = self._factory(self._host, self._port, timeout=self._timeout)
            conn.login(self._user, self._password)
            typ, _ = conn.select(f'"{folder}"', readonly=True)  # EXAMINE – jen čtení
            if typ != "OK":
                raise MailError(f"Složku {folder} nelze otevřít.")
            return conn
        except imaplib.IMAP4.error as e:
            raise MailError(f"Přihlášení k iCloud Mail selhalo: {e}") from e
        except OSError as e:
            raise MailError(f"iCloud Mail nedostupný: {e}") from e

    @staticmethod
    def _close(conn: Any) -> None:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001, S110
            pass

    def _headers_sync(self, folder_code: str, since: datetime, limit: int) -> list[MailHeader]:
        conn = self._connect(FOLDER_CODES[folder_code])
        try:
            typ, data = conn.uid("SEARCH", None, f"SINCE {imap_since(since)}")
            if typ != "OK":
                raise MailError("Vyhledávání v poště selhalo.")
            uids = [int(u) for u in (data[0] or b"").split()][-limit:]
            if not uids:
                return []
            uid_set = ",".join(str(u) for u in uids)
            typ, data = conn.uid(
                "FETCH",
                uid_set,
                f"(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])",
            )
            if typ != "OK":
                raise MailError("Načtení hlaviček selhalo.")
            headers = []
            for meta, raw in _fetch_items(data):
                uid = _meta_int(meta, "UID")
                if uid is None:
                    continue
                headers.append(
                    parse_header(
                        raw,
                        folder_code,
                        uid,
                        seen=b"\\Seen" in meta,
                        size=_meta_int(meta, "RFC822.SIZE") or 0,
                    )
                )
            return headers
        finally:
            self._close(conn)

    def _message_sync(self, folder_code: str, uid: int) -> MailMessage:
        conn = self._connect(FOLDER_CODES[folder_code])
        try:
            typ, data = conn.uid("FETCH", str(uid), "(UID FLAGS BODY.PEEK[])")
            items = list(_fetch_items(data)) if typ == "OK" else []
            if not items:
                raise MailError(f"Zpráva E-{folder_code}{uid} neexistuje.")
            meta, raw = items[0]
            return parse_message(raw, folder_code, uid, seen=b"\\Seen" in meta)
        finally:
            self._close(conn)

    def _snippets_sync(self, folder_code: str, uids: list[int]) -> dict[int, str]:
        if not uids:
            return {}
        conn = self._connect(FOLDER_CODES[folder_code])
        try:
            out: dict[int, str] = {}
            for i in range(0, len(uids), 20):
                uid_set = ",".join(str(u) for u in uids[i : i + 20])
                typ, data = conn.uid("FETCH", uid_set, "(UID BODY.PEEK[])")
                if typ != "OK":
                    raise MailError("Načtení náhledů selhalo.")
                for meta, raw in _fetch_items(data):
                    uid = _meta_int(meta, "UID")
                    if uid is not None:
                        out[uid] = snippet_of(raw, folder_code, uid)
            return out
        finally:
            self._close(conn)

    async def headers_since(
        self, folder_code: str, since: datetime, limit: int
    ) -> list[MailHeader]:
        import asyncio

        return await asyncio.to_thread(self._headers_sync, folder_code, since, limit)

    async def snippets(self, folder_code: str, uids: list[int]) -> dict[int, str]:
        """Krátké náhledy textu (BODY.PEEK – zprávy zůstanou nepřečtené)."""
        import asyncio

        return await asyncio.to_thread(self._snippets_sync, folder_code, uids)

    async def message(self, folder_code: str, uid: int) -> MailMessage:
        import asyncio

        return await asyncio.to_thread(self._message_sync, folder_code, uid)


def _fetch_items(data: list[Any]) -> list[tuple[bytes, bytes]]:
    """Z odpovědi imaplib FETCH vybere dvojice (metadata, obsah)."""
    out = []
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            out.append((bytes(item[0]), bytes(item[1])))
    return out


def _meta_int(meta: bytes, key: str) -> int | None:
    m = re.search(rb"\b" + re.escape(key.encode()) + rb" (\d+)", meta)
    return int(m.group(1)) if m else None


def since_hours(hours: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(hours=hours)
