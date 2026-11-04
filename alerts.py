"""Email alerts to the Admin (server side only).

Off until these environment variables are set in Vercel:
  SMTP_USER       – the Gmail address that sends the alerts
  SMTP_PASSWORD   – a Gmail "App password" for that address (not the normal password)
  ALERT_EMAIL_TO  – who receives them (comma separated); defaults to SMTP_USER
  SMTP_HOST / SMTP_PORT – default smtp.gmail.com / 465
Alerts never contain passwords, keys or applicant documents.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage

SENT = []          # tests replace send() or read this list


def configured():
    return bool(os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"))


def recipients():
    raw = os.getenv("ALERT_EMAIL_TO") or os.getenv("SMTP_USER") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


def send(subject, body):
    """Send one email. Returns True if it went out; never raises."""
    to = recipients()
    if not (configured() and to):
        return False
    msg = EmailMessage()
    msg["Subject"] = "[Tooba Loan Dept] " + subject[:150]
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = ", ".join(to)
    msg.set_content(body[:5000])
    try:
        host, port = os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465"))
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=8) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
        SENT.append(subject)
        return True
    except Exception:
        return False
