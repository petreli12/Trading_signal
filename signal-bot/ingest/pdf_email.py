"""EOD PDF ingestion via IMAP.

The recap PDF arrives by email only — a human forwards it to a dedicated
ingestion inbox. This module connects via IMAP, finds the most recent message
with a PDF attachment, saves it, and extracts text with pymupdf.

NEVER read Discord directly. The only Discord interaction is the human forward.
"""

import email
import imaplib
import os
import re
from datetime import date, datetime, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import TypedDict

import fitz  # pymupdf

# Matches a recap date line like "May 28, 2026" near the top of the PDF.
_RECAP_DATE_RE = re.compile(
    r"\b("
    r"January|February|March|April|May|June|July|"
    r"August|September|October|November|December"
    r")\s+\d{1,2},\s+\d{4}\b"
)

DEFAULT_MAILBOX = "INBOX"
DEFAULT_SAVE_DIR = os.environ.get("PDF_SAVE_DIR", "data/pdfs")
# How many of the most recent messages to scan for a PDF attachment.
DEFAULT_SCAN_LIMIT = 25


class PdfDoc(TypedDict):
    doc_date: str
    source: str
    raw_text: str


def _config() -> tuple[str, str, str]:
    # .strip() guards against trailing newlines pasted into CI secrets.
    host = (os.environ.get("IMAP_HOST") or "").strip()
    user = (os.environ.get("IMAP_USER") or "").strip()
    password = (os.environ.get("IMAP_PASSWORD") or "").strip()
    missing = [
        name
        for name, val in (
            ("IMAP_HOST", host),
            ("IMAP_USER", user),
            ("IMAP_PASSWORD", password),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Missing IMAP config: {', '.join(missing)}. Set them in .env / environment."
        )
    return host, user, password  # type: ignore[return-value]


def _decode(value: str | None) -> str:
    """Decode a possibly RFC 2047-encoded header (e.g. filename) to str."""
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _pdf_attachments(msg: Message) -> list[tuple[str, bytes]]:
    """Return (filename, bytes) for each PDF attachment in a message."""
    found: list[tuple[str, bytes]] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = _decode(part.get_filename())
        content_type = (part.get_content_type() or "").lower()
        is_pdf = content_type == "application/pdf" or filename.lower().endswith(".pdf")
        if not is_pdf:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            found.append((filename or "attachment.pdf", payload))
    return found


def _extract_text(data: bytes) -> str:
    """Extract text from PDF bytes using pymupdf."""
    with fitz.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def _recap_date_from_text(text: str) -> str | None:
    """Parse the recap's own coverage date (e.g. 'May 28, 2026') from the PDF.

    This is the true trading day the recap covers — which can differ from when
    the email was forwarded (e.g. a Friday recap forwarded over the weekend).
    Day-over-day deltas key off this date, so prefer it over the email date.
    """
    match = _RECAP_DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def _email_date(msg: Message) -> str:
    """Fallback recap date from the message Date header (UTC, YYYY-MM-DD)."""
    raw = msg.get("Date")
    if raw:
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc).date().isoformat()
        except (TypeError, ValueError):
            pass
    return date.today().isoformat()


def fetch(
    mailbox: str = DEFAULT_MAILBOX,
    save_dir: str = DEFAULT_SAVE_DIR,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
) -> PdfDoc | None:
    """Find the newest forwarded PDF over IMAP and extract its text.

    Scans up to ``scan_limit`` most-recent messages, returning the first one
    that carries a PDF attachment. The PDF is also saved under ``save_dir``.

    Returns:
        A ``PdfDoc`` with extracted text, or ``None`` if no matching message
        with a PDF attachment was found.

    Note:
        Reads IMAP_HOST / IMAP_USER / IMAP_PASSWORD from the environment.
        IMAP_PASSWORD is an app-specific password.
    """
    host, user, password = _config()
    os.makedirs(save_dir, exist_ok=True)

    with imaplib.IMAP4_SSL(host) as imap:
        imap.login(user, password)
        imap.select(mailbox)

        status, data = imap.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return None

        # Search returns ids oldest-first; scan newest-first.
        ids = data[0].split()
        for msg_id in reversed(ids[-scan_limit:]):
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            raw_bytes = next(
                (part[1] for part in msg_data if isinstance(part, tuple)), None
            )
            if not raw_bytes:
                continue

            msg = email.message_from_bytes(raw_bytes)
            attachments = _pdf_attachments(msg)
            if not attachments:
                continue

            filename, pdf_bytes = attachments[0]
            raw_text = _extract_text(pdf_bytes)
            # Prefer the recap's own date; fall back to the email's Date header.
            doc_date = _recap_date_from_text(raw_text) or _email_date(msg)

            safe_name = f"{doc_date}_{os.path.basename(filename)}"
            with open(os.path.join(save_dir, safe_name), "wb") as fh:
                fh.write(pdf_bytes)

            return PdfDoc(
                doc_date=doc_date,
                source=_decode(msg.get("From")) or filename,
                raw_text=raw_text,
            )

    return None
