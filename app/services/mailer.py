"""Outgoing email via SMTP, with a console + instance/outbox.log fallback."""
import logging
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from app.config import settings
from app.dates import format_ist_stamp

log = logging.getLogger("nest5.mailer")


def smtp_configured() -> bool:
    return bool(settings.smtp_host and (settings.smtp_from or settings.smtp_user))


def outbox_path() -> Path:
    return Path(settings.instance_dir) / "outbox.log"


def _write_outbox(to: str, subject: str, body: str, reason: str) -> None:
    path = outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = format_ist_stamp()
    entry = (
        f"===== {stamp} | to: {to} | {reason}\n"
        f"Subject: {subject}\n\n{body}\n\n"
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(entry)
    print(f"\n[nest5 mail] {reason} — message for {to} written to {path}\n{subject}\n{body}\n", flush=True)


def send_email(to: str, subject: str, body: str) -> bool:
    """Send an email. Returns True if delivered over SMTP, False if written to the outbox."""
    if not smtp_configured():
        _write_outbox(to, subject, body, "SMTP not configured")
        return False
    msg = EmailMessage()
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        context = ssl.create_default_context()
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=20) as smtp:
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001 — never block recovery on mail errors
        log.warning("SMTP send failed: %s", exc)
        _write_outbox(to, subject, body, f"SMTP send failed ({exc.__class__.__name__}: {exc})")
        return False
