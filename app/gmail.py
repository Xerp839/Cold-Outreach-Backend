"""Puts emails into Gmail's Drafts folder over IMAP (APPEND). Nothing is ever sent from here."""

import imaplib
import re
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

IMAP_HOST = "imap.gmail.com"
_FALLBACK_DRAFTS = "[Gmail]/Drafts"


def build_message(from_addr: str, from_name: str, to_addr: str, to_name: str | None, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = formataddr((to_name, to_addr)) if to_name else to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
    msg.set_content(body)
    return msg


def _drafts_folder(conn: imaplib.IMAP4_SSL) -> str:
    """Finds the folder flagged \\Drafts (its name changes with the Gmail UI language)."""
    typ, folders = conn.list()
    if typ == "OK":
        for raw in folders or []:
            line = raw.decode(errors="ignore")
            if "\\Drafts" in line:
                m = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
                if m:
                    return m.group(1)
    return _FALLBACK_DRAFTS


class GmailDrafts:
    def __init__(self, address: str, app_password: str):
        self.address = address
        self.app_password = app_password.replace(" ", "")  # Google shows app passwords in groups of 4
        self._conn: imaplib.IMAP4_SSL | None = None
        self._folder: str | None = None

    def __enter__(self) -> "GmailDrafts":
        self._conn = imaplib.IMAP4_SSL(IMAP_HOST)
        self._conn.login(self.address, self.app_password)
        self._folder = _drafts_folder(self._conn)
        return self

    def __exit__(self, *exc) -> None:
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass

    def save_draft(self, msg: EmailMessage) -> None:
        assert self._conn and self._folder, "use GmailDrafts as a context manager"
        typ, data = self._conn.append(
            f'"{self._folder}"', "(\\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes()
        )
        if typ != "OK":
            raise RuntimeError(f"IMAP APPEND failed: {data}")

    def check(self) -> str:
        """Login test used by the /health/gmail endpoint."""
        return self._folder or ""
