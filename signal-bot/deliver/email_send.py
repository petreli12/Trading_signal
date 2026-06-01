"""Delivery: send the brief via SMTP.

The brief is Markdown (from ``summarize.summarize``); this module sends a
multipart email with a plaintext part (the raw Markdown) and an HTML part
(rendered, so tables and headings display in email clients).
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

import markdown as _markdown

DEFAULT_SMTP_PORT = 587
_MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "nl2br"]

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

    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(user, password)
            smtp.send_message(msg)
