import re
import unicodedata
from functools import lru_cache
from urllib.parse import urlparse

import dns.exception
import dns.resolver

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JUNK_LOCAL_PARTS = ("noreply", "no-reply", "donotreply", "do-not-reply", "privacy", "abuse", "support", "billing", "legal")
# Free-mail and job-board domains are never the company's own domain.
NON_COMPANY_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com", "rediffmail.com", "icloud.com",
    "linkedin.com", "naukri.com", "indeed.com", "instahyre.com", "cutshort.io", "wellfound.com", "glassdoor.com",
    "lever.co", "greenhouse.io", "ashbyhq.com", "workable.com", "myworkdayjobs.com", "myworkdaysite.com",
}


def extract_emails(text: str | None) -> list[str]:
    if not text:
        return []
    seen, out = set(), []
    for m in EMAIL_RE.findall(text):
        email = m.strip(".").lower()
        local = email.split("@")[0]
        if email in seen or any(j in local for j in JUNK_LOCAL_PARTS) or email.endswith((".png", ".jpg")):
            continue
        seen.add(email)
        out.append(email)
    return out


def domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url if "://" in url else f"https://{url}").hostname or ""
    host = host.lower().removeprefix("www.")
    if not host or any(host == d or host.endswith("." + d) for d in NON_COMPANY_DOMAINS):
        return None
    return host


def clean_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    return domain_from_url(domain.strip())


@lru_cache(maxsize=512)
def has_mx(domain: str) -> bool:
    try:
        return len(dns.resolver.resolve(domain, "MX", lifetime=5)) > 0
    except (dns.exception.DNSException, OSError):
        return False


def _ascii_name(part: str | None) -> str:
    if not part:
        return ""
    part = unicodedata.normalize("NFKD", part).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", part.lower())


def apply_pattern(pattern: str, first_name: str | None, last_name: str | None, domain: str) -> str | None:
    """Hunter patterns look like '{first}.{last}', '{f}{last}', '{first}'."""
    first, last = _ascii_name(first_name), _ascii_name(last_name)
    needs_last = "{last}" in pattern or "{l}" in pattern
    if not first or (needs_last and not last):
        return None
    local = (
        pattern.replace("{first}", first)
        .replace("{last}", last)
        .replace("{f}", first[:1])
        .replace("{l}", last[:1])
    )
    if "{" in local or not local:
        return None
    return f"{local}@{domain}"


def first_name_from_email(email: str) -> str | None:
    """'priya.sharma@x.com' -> 'Priya'. Generic inboxes (careers@, hr@) return None."""
    local = email.split("@")[0].lower()
    generic = ("career", "job", "hr", "hiring", "recruit", "talent", "people", "info", "contact", "hello", "team", "resume", "cv")
    if any(local.startswith(g) for g in generic):
        return None
    token = re.split(r"[._\-+0-9]", local)[0]
    return token.capitalize() if len(token) >= 3 else None
