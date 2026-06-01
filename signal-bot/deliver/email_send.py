"""Delivery: send the brief via SMTP.

The brief is Markdown (from ``summarize.summarize``); this module sends a
multipart email with a plaintext part (the raw Markdown) and an HTML part
(rendered, so tables and headings display in email clients).
"""

import os
import re
import smtplib
import ssl
from email.message import EmailMessage

import markdown as _markdown

DEFAULT_SMTP_PORT = 587
_MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "nl2br"]

# US carrier email-to-SMS gateways. Send an email to <digits>@<gateway> and it
# arrives as a text. Free, but carrier-dependent and some carriers are phasing
# them out — set SMS_TO directly to override if a carrier isn't listed here.
_SMS_GATEWAYS = {
    "verizon": "vtext.com",
    "att": "txt.att.net",
    "tmobile": "tmomail.net",
    "googlefi": "msg.fi.google.com",
    "uscellular": "email.uscc.net",
    "cricket": "sms.cricketwireless.net",
    "boost": "sms.myboostmobile.com",
    "metropcs": "mymetropcs.com",
    "metro": "mymetropcs.com",
    "virgin": "vmobl.com",
    "xfinity": "vtext.com",
}

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
          color: #1a1a1a; line-height: 1.5; max-width: 760px; margin: 0 auto; }}
  h1 {{ font-size: 20px; }} h2 {{ font-size: 16px; margin-top: 1.4em; }}
  h3 {{ font-size: 14px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; }}
  th {{ background: #f4f4f4; }}
  hr {{ border: none; border-top: 1px solid #eee; margin: 16px 0; }}
  code {{ background: #f4f4f4; padding: 1px 4px; border-radius: 3px; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def _config() -> tuple[str, int, str, str, list[str]]:
    # .strip() guards against trailing newlines pasted into CI secrets, which
    # would otherwise produce invalid email headers or failed logins.
    host = (os.environ.get("SMTP_HOST") or "").strip()
    user = (os.environ.get("SMTP_USER") or "").strip()
    password = (os.environ.get("SMTP_PASSWORD") or "").strip()
    # An unset GitHub Actions secret resolves to "", so fall back explicitly.
    port = int((os.environ.get("SMTP_PORT") or "").strip() or DEFAULT_SMTP_PORT)
    recipients = [r.strip() for r in os.environ.get("EMAIL_TO", "").split(",") if r.strip()]

    missing = [
        name
        for name, val in (
            ("SMTP_HOST", host),
            ("SMTP_USER", user),
            ("SMTP_PASSWORD", password),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Missing SMTP config: {', '.join(missing)}. Set them in .env / environment."
        )
    return host, port, user, password, recipients  # type: ignore[return-value]


def _deliver(msg: EmailMessage, host: str, port: int, user: str, password: str) -> None:
    """Open an SMTP connection (SSL or STARTTLS) and send one message."""
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(user, password)
            smtp.send_message(msg)


def render_html(markdown_text: str) -> str:
    """Render Markdown (incl. tables) to a standalone, styled HTML document."""
    body_html = _markdown.markdown(markdown_text, extensions=_MD_EXTENSIONS)
    return _HTML_TEMPLATE.format(body=body_html)


def send(subject: str, body: str, to: list[str] | str | None = None) -> None:
    """Send the brief to the configured recipient(s) over SMTP.

    Args:
        subject: Email subject line.
        body: The brief content in Markdown. Sent as both plaintext (raw) and
            rendered HTML.
        to: Optional recipient override (str or list). Defaults to EMAIL_TO.

    Note:
        Reads SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / EMAIL_TO from
        the environment. SMTP_PASSWORD is an app-specific password.
    """
    host, port, user, password, default_to = _config()

    if to is None:
        recipients = default_to
    elif isinstance(to, str):
        recipients = [to]
    else:
        recipients = list(to)
    if not recipients:
        raise RuntimeError("No recipients: set EMAIL_TO or pass `to`.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    msg.add_alternative(render_html(body), subtype="html")

    _deliver(msg, host, port, user, password)


def _sms_recipients() -> list[str]:
    """Resolve email-to-SMS gateway address(es) from the environment.

    Either SMS_TO (one or more full gateway addresses, comma-separated) or
    SMS_PHONE (one or more numbers, comma-separated) + a single SMS_CARRIER.
    Returns an empty list when SMS is not configured.
    """
    explicit = [a.strip() for a in (os.environ.get("SMS_TO") or "").split(",") if a.strip()]
    if explicit:
        return explicit

    carrier = (os.environ.get("SMS_CARRIER") or "").strip().lower()
    gateway = _SMS_GATEWAYS.get(carrier)
    if not gateway:
        return []

    out: list[str] = []
    for raw in (os.environ.get("SMS_PHONE") or "").split(","):
        phone = re.sub(r"\D", "", raw)
        if not phone:
            continue
        # Gateways expect 10-digit US numbers (strip a leading country code 1).
        if len(phone) == 11 and phone.startswith("1"):
            phone = phone[1:]
        out.append(f"{phone}@{gateway}")
    return out


def sms_enabled() -> bool:
    """True if at least one usable SMS gateway recipient is configured."""
    return bool(_sms_recipients())


def send_sms(text: str) -> None:
    """Send a short plaintext message via the carrier email-to-SMS gateway.

    Reuses the SMTP credentials. Plaintext only and no HTML part — gateways
    discard rich content and many fold the subject into the message body, so
    the subject is left empty and the whole text goes in the body.

    Raises:
        RuntimeError: if SMS is not configured (no SMS_TO / SMS_PHONE+CARRIER).
    """
    recipients = _sms_recipients()
    if not recipients:
        raise RuntimeError("SMS not configured: set SMS_TO or SMS_PHONE + SMS_CARRIER.")
    host, port, user, password, _ = _config()

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = ""
    msg.set_content(text)

    _deliver(msg, host, port, user, password)
