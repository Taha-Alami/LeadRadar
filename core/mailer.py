"""
Email dispatch for LeadRadar, with three interchangeable backends selected by
``MAIL_BACKEND``:

* ``file``  (default) — writes each message as an ``.html`` file into
  ``MAIL_OUTBOX_DIR`` (``./outbox``). Zero setup; ideal for local demos.
* ``smtp``  — any SMTP relay (Gmail app password, SendGrid, Mailgun, an
  internal relay…).
* ``graph`` — Microsoft Graph ``sendMail`` over HTTPS via an Entra ID app
  registration with the ``Mail.Send`` application permission. Useful on
  platforms that block outbound SMTP ports (e.g. Azure Container Apps).

Every backend sits behind one function, ``send_email()``, which returns
``True``/``False`` instead of raising — a mail outage never crashes a pipeline
run half-way through.
"""
from __future__ import annotations

import base64
import html as html_lib
import json
import logging
import mimetypes
import os
import re
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Callable

from core.config import AppConfig

logger = logging.getLogger(__name__)

_GRAPH_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_GRAPH_SEND_URL  = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"


# ── Backends ─────────────────────────────────────────────────────────────────

def _send_file(cfg: AppConfig, subject: str, html_body: str, to: list[str], cc: list[str], attachments: list[str]) -> None:
    """Write the message to the outbox directory — open it in a browser to preview."""
    outbox = Path(cfg.mail_outbox_dir)
    outbox.mkdir(parents=True, exist_ok=True)
    slug  = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:60] or "message"
    path  = outbox / f"{datetime.now():%Y%m%d-%H%M%S}_{slug}.html"
    header = (
        f"<!--\n  To: {', '.join(to) or '(none)'}\n  Cc: {', '.join(cc) or '(none)'}\n"
        f"  Subject: {subject}\n  Attachments: {', '.join(attachments) or '(none)'}\n-->\n"
    )
    path.write_text(header + html_body, encoding="utf-8")
    logger.info("Mail written to outbox: %s", path)


def _send_smtp(cfg: AppConfig, subject: str, html_body: str, to: list[str], cc: list[str], attachments: list[str]) -> None:
    if not cfg.smtp_host:
        raise ValueError("MAIL_BACKEND=smtp but SMTP_HOST is not set.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = cfg.email_sender
    msg["To"]      = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content("This message contains HTML content — please view it in an HTML-capable mail client.")
    msg.add_alternative(html_body, subtype="html")

    for file_path in attachments:
        if not os.path.exists(file_path):
            logger.warning("Attachment not found — skipping: %s", file_path)
            continue
        ctype, _ = mimetypes.guess_type(file_path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(file_path, "rb") as fh:
            msg.add_attachment(fh.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(file_path))

    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
        if cfg.smtp_use_tls:
            smtp.starttls()
        if cfg.smtp_username:
            smtp.login(cfg.smtp_username, cfg.smtp_password)
        smtp.send_message(msg)


def _graph_token(cfg: AppConfig) -> str:
    url  = _GRAPH_TOKEN_URL.format(tenant=cfg.graph_tenant_id)
    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     cfg.graph_client_id,
        "client_secret": cfg.graph_client_secret,
        "scope":         "https://graph.microsoft.com/.default",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["access_token"]


def _send_graph(cfg: AppConfig, subject: str, html_body: str, to: list[str], cc: list[str], attachments: list[str]) -> None:
    if not (cfg.graph_tenant_id and cfg.graph_client_id and cfg.graph_client_secret):
        raise ValueError("MAIL_BACKEND=graph but GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET are not all set.")

    message: dict = {
        "subject":      subject,
        "body":         {"contentType": "HTML", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
    }
    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]

    if attachments:
        message["attachments"] = []
        for file_path in attachments:
            if not os.path.exists(file_path):
                logger.warning("Attachment not found — skipping: %s", file_path)
                continue
            with open(file_path, "rb") as fh:
                encoded = base64.b64encode(fh.read()).decode()
            message["attachments"].append({
                "@odata.type":  "#microsoft.graph.fileAttachment",
                "name":         os.path.basename(file_path),
                "contentBytes": encoded,
            })

    payload = json.dumps({"message": message, "saveToSentItems": False}).encode()
    url     = _GRAPH_SEND_URL.format(sender=urllib.parse.quote(cfg.email_sender))
    req     = urllib.request.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {_graph_token(cfg)}",
        "Content-Type":  "application/json",
    })
    with urllib.request.urlopen(req, timeout=30):
        pass   # HTTP 202 Accepted = success, no body


_BACKENDS: dict[str, Callable[..., None]] = {
    "file":  _send_file,
    "smtp":  _send_smtp,
    "graph": _send_graph,
}


# ── Public API ───────────────────────────────────────────────────────────────

def send_email(
    cfg:         AppConfig,
    subject:     str,
    html_body:   str,
    *,
    recipients:  list[str] | None = None,
    cc:          list[str] | None = None,
    attachments: list[str] | None = None,
) -> bool:
    """
    Send an HTML email through the configured backend.

    Args:
        cfg:         Loaded ``AppConfig`` — selects the backend and supplies
                     sender address and credentials.
        subject:     Email subject line.
        html_body:   Full HTML string for the email body.
        recipients:  Override ``cfg.email_recipients``.
        cc:          Override ``cfg.email_cc``.
        attachments: Optional list of local file paths to attach.

    Returns:
        ``True`` if the backend accepted the message, ``False`` otherwise.
    """
    to_list = recipients if recipients is not None else list(cfg.email_recipients)
    cc_list = cc         if cc         is not None else list(cfg.email_cc)

    backend = _BACKENDS.get(cfg.mail_backend)
    if backend is None:
        logger.error("Unknown MAIL_BACKEND=%r — expected one of: %s", cfg.mail_backend, ", ".join(_BACKENDS))
        return False
    if not to_list and cfg.mail_backend != "file":
        logger.error("No recipients — set EMAIL_RECIPIENTS (or RECIPIENTS_<COUNTRY>) in .env.")
        return False

    try:
        backend(cfg, subject, html_body, to_list, cc_list, attachments or [])
        logger.info("Email dispatched via %s → %s", cfg.mail_backend, ", ".join(to_list) or "(outbox)")
        return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        logger.error("Graph mail dispatch failed (HTTP %s): %s", exc.code, body)
        if exc.code == 403:
            logger.error(
                "HTTP 403 from Graph usually means the Mail.Send application permission "
                "is missing or admin consent has not been granted on the app registration."
            )
        return False
    except Exception as exc:
        logger.error("Mail dispatch via %s failed: %s", cfg.mail_backend, exc)
        return False


def send_error_alert(cfg: AppConfig, pipeline_name: str, error: str, stack: str = "") -> None:
    """
    Send a minimal HTML error alert to the default recipients.

    Called from the entry points when an unhandled exception terminates a run,
    so operators are notified even if the run produced no output.
    """
    stack_block = (
        f"<pre style='background:#F5F5F5;padding:12px;font-size:11px;"
        f"overflow:auto;border-radius:4px;'>{html_lib.escape(stack)}</pre>"
        if stack else ""
    )
    html = f"""
<div style='font-family:Arial,sans-serif;max-width:640px;padding:24px;'>
  <h2 style='color:#B71C1C;margin:0 0 16px;'>{html_lib.escape(pipeline_name)} — Pipeline Error</h2>
  <div style='background:#FFEBEE;border-left:4px solid #C62828;padding:12px 16px;
              font-size:13px;border-radius:2px;'>
    <strong>Error:</strong> {html_lib.escape(error)}
  </div>
  {stack_block}
</div>
"""
    send_email(cfg, subject=f"[ERROR] {pipeline_name} — Pipeline failed", html_body=html)
