import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import os

logger = logging.getLogger("eloise")

# Env var aliases: code historically read SMTP_PASSWORD/MAIL_FROM but .env and
# .env.example ship SMTP_PASS/FROM_EMAIL. Read both, preferring the shorter
# names that match the shipped config.
_PASSWORD_KEYS = ("SMTP_PASS", "SMTP_PASSWORD")
_FROM_KEYS = ("FROM_EMAIL", "MAIL_FROM")
_NAME_KEYS = ("FROM_NAME", "MAIL_FROM_NAME")


def _cfg(key, default=None):
    return os.environ.get(key) or default


def _cfg_any(keys, default=None):
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    return default


def smtp_password():
    return _cfg_any(_PASSWORD_KEYS)


def smtp_from():
    return _cfg_any(_FROM_KEYS) or _cfg("SMTP_USER") or "eloise@localhost"


def smtp_from_name():
    return _cfg_any(_NAME_KEYS) or "Eloise"


def smtp_configured():
    # "configured" means we have enough to actually attempt a login, not just
    # a hostname. A bare SMTP_HOST without credentials will always fail.
    return bool(_cfg("SMTP_HOST") and _cfg("SMTP_USER") and smtp_password())


def smtp_diag():
    """Structured config report used by the Settings screen. Never returns secrets."""
    host = _cfg("SMTP_HOST")
    user = _cfg("SMTP_USER")
    password = smtp_password()
    port = int(_cfg("SMTP_PORT") or "587")
    tls = bool(int(_cfg("SMTP_TLS") or "1"))
    problems = []
    if not host:
        problems.append("SMTP_HOST is not set.")
    if not user:
        problems.append("SMTP_USER is not set.")
    if not password:
        problems.append(
            "No password found — set SMTP_PASS (16-char Gmail App Password) in your .env."
        )
    elif host and "gmail" in host.lower() and len(password) != 16:
        problems.append(
            f"Gmail password is {len(password)} chars — Gmail needs a 16-char App Password "
            "(Google Account > Security > 2-Step Verification > App passwords)."
        )
    return {
        "configured": not problems,
        "host": host,
        "port": port,
        "user": user,
        "from": smtp_from(),
        "from_name": smtp_from_name(),
        "tls": tls,
        "password_set": bool(password),
        "problems": problems,
    }


def send_email(to, subject, text="", html=None):
    ok, _err = send_email_detailed(to, subject, text, html)
    return ok


def send_email_detailed(to, subject, text="", html=None):
    """Send mail. Returns (ok, error_message) so callers can surface the real reason."""
    host = _cfg("SMTP_HOST")
    port = int(_cfg("SMTP_PORT") or "587")
    user = _cfg("SMTP_USER")
    password = smtp_password()
    _from = smtp_from()
    _name = smtp_from_name()
    if not host:
        return False, "SMTP_HOST is not set."
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
        use_ssl = port == 465
        if use_ssl:
            smtp = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            smtp = smtplib.SMTP(host, port, timeout=20)
        with smtp:
            if not use_ssl and int(_cfg("SMTP_TLS") or "1"):
                smtp.starttls()
            if user:
                if not password:
                    return False, "SMTP_USER is set but no password (set SMTP_PASS in .env)."
                smtp.login(user, password)
            smtp.sendmail(_from, [to], msg.as_string())
        return True, ""
    except smtplib.SMTPAuthenticationError as exc:
        detail = (exc.smtp_error or b"").decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes) else str(exc)
        hint = ""
        if host and "gmail" in host.lower():
            hint = " — Gmail rejects normal passwords; use a 16-char App Password."
        return False, f"Login rejected by {host}: {detail}{hint}"
    except Exception as exc:
        logger.warning("mail to %s failed: %s", to, exc)
        return False, f"{type(exc).__name__}: {exc}"
