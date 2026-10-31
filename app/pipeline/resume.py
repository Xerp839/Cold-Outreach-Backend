"""Reads the resume text from the same Drive link that goes into emails, so there's one source of truth."""

import io
import re
from datetime import timedelta

import httpx
from pypdf import PdfReader
from sqlalchemy import select

from app.db import session_scope
from app.models import ResumeCache, as_aware, utcnow

REFRESH_AFTER = timedelta(hours=12)


def drive_download_url(share_url: str) -> str:
    m = re.search(r"/d/([A-Za-z0-9_-]+)", share_url) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", share_url)
    if not m:
        return share_url  # not a Drive link - assume it's a direct PDF URL
    return f"https://drive.google.com/uc?export=download&id={m.group(1)}"


def _download_text(share_url: str) -> str:
    resp = httpx.get(drive_download_url(share_url), follow_redirects=True, timeout=30)
    resp.raise_for_status()
    if not resp.content.startswith(b"%PDF"):
        raise ValueError("resume link did not return a PDF - is the Drive file shared as 'Anyone with the link'?")
    reader = PdfReader(io.BytesIO(resp.content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) < 200:
        raise ValueError("resume PDF has almost no extractable text")
    return text


def get_resume_text(share_url: str) -> str:
    with session_scope() as s:
        cached = s.scalar(select(ResumeCache).order_by(ResumeCache.fetched_at.desc()))
        fresh = cached and cached.source_url == share_url and utcnow() - as_aware(cached.fetched_at) < REFRESH_AFTER
        if fresh:
            return cached.text
        try:
            text = _download_text(share_url)
        except Exception:
            if cached:  # Drive hiccup: an older copy beats failing the whole run
                return cached.text
            raise
        if cached:
            cached.source_url, cached.text, cached.fetched_at = share_url, text, utcnow()
        else:
            s.add(ResumeCache(source_url=share_url, text=text))
        return text
