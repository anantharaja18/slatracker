"""
Sends the SLA reports by SMTP (Office 365). Settings come from .env:

    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_SENDER_NAME,
    SMTP_TO        recipients for the live reports, separated by ";"
    SMTP_TEST_TO   the only recipient the "send test email" button uses

Nothing here stores or logs the password.
"""

import os
import re
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid

INLINE_PNG_CID = "sla_summary"


def _split(value):
    """Recipients from a ";" or "," separated string. Stray '<' '>' and blanks are dropped."""
    out, seen = [], set()
    for part in value.replace(",", ";").split(";"):
        addr = part.strip().strip("<>").strip()
        if addr and addr.lower() not in seen:
            seen.add(addr.lower())
            out.append(addr)
    return out


ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def split_recipients(text):
    return _split(text)


def valid_address(address):
    return bool(ADDRESS_RE.match(address))


def live_recipients():
    """Recipients for the live hourly and EOD emails: the list saved in the dashboard's admin
    panel if there is one, otherwise SMTP_TO from .env."""
    import report_settings
    saved = report_settings.load().get("email_recipients")
    if saved is not None:
        return list(saved)
    return _split(os.environ.get("SMTP_TO", ""))


def test_recipients():
    return _split(os.environ.get("SMTP_TEST_TO", ""))


def configured():
    return all(os.environ.get(k, "").strip() for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"))


def send(subject, html_body, to, png=None, attachment=None):
    """Sends one email to `to` (a list of addresses).
    png: bytes shown inside the body (referenced as cid:sla_summary).
    attachment: (file_name, bytes) for the Excel file."""
    if not configured():
        raise RuntimeError("SMTP settings are missing in .env")
    if not to:
        raise RuntimeError("No recipients are set")

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = formataddr((os.environ.get("SMTP_SENDER_NAME", "").strip(), os.environ["SMTP_FROM"].strip()))
    msg["To"] = "; ".join(to)
    msg["Message-ID"] = make_msgid(domain=os.environ["SMTP_FROM"].split("@")[-1])

    related = MIMEMultipart("related")
    related.attach(MIMEText(html_body, "html", "utf-8"))
    if png:
        image = MIMEImage(png, "png")
        image.add_header("Content-ID", f"<{INLINE_PNG_CID}>")
        image.add_header("Content-Disposition", "inline", filename="sla_summary.png")
        related.attach(image)
    msg.attach(related)

    if attachment:
        file_name, data = attachment
        part = MIMEApplication(data, Name=file_name)
        part.add_header("Content-Disposition", "attachment", filename=file_name)
        msg.attach(part)

    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    with smtplib.SMTP(os.environ["SMTP_HOST"].strip(), port, timeout=90) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(os.environ["SMTP_USER"].strip(), os.environ["SMTP_PASSWORD"])
        server.sendmail(os.environ["SMTP_FROM"].strip(), to, msg.as_string())
    print(f"[smtp_mail] Sent '{subject}' to {len(to)} recipient(s)")
