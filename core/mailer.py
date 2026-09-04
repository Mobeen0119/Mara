"""Real SMTP delivery for Eloise digests, confirmations, and schedule emails.

Configure in .env / app settings:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, MAIL_FROM_NAME
A schedule/digest email is only sent when an SMTP host is configured AND the
user has an email on file. Sends are best-effort: failures are logged, never
crash the request.
"""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import os

logger = logging.getLogger("eloise")


def _cfg(key, default=None):
    return os.environ.get(key) or default


def smtp_configured():
    return bool(_cfg("SMTP_HOST"))


def send_email(to, subject, text="", html=None):
    host = _cfg("SMTP_HOST")
    port = int(_cfg("SMTP_PORT") or "587")
    user = _cfg("SMTP_USER")
    password = _cfg("SMTP_PASSWORD")
    _from = _cfg("MAIL_FROM") or (user or "eloise@localhost")
    _name = _cfg("MAIL_FROM_NAME") or "Eloise"
    if not host:
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{_name} <{_from}>"
    msg["To"] = to
    if html:
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
    else:
        msg.attach(MIMEText(text or "", "plain"))
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            if int(_cfg("SMTP_TLS") or 1):
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.sendmail(_from, [to], msg.as_string())
        return True
    except Exception as exc:
        logger.warning("mail to %s failed: %s", to, exc)
        return False