"""Contact finders: Apollo (people search + email reveal) and Hunter (domain search + email pattern)."""

import re
from dataclasses import dataclass, field

import httpx

from app.config import get_settings
from app.providers.llm import ProviderError, ProviderExhausted
from app.tracking import calls_today, record_usage

TARGET_TITLES = [
    "talent acquisition", "technical recruiter", "recruiter", "recruitment", "hiring",
    "hr", "human resources", "people",
    "engineering manager", "head of engineering", "head of ai", "vp engineering", "cto",
    "founder", "co-founder", "ceo",
]


@dataclass
class Candidate:
    source: str
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    confidence: int | None = None
    linkedin_url: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def name(self) -> str | None:
        full = " ".join(p for p in (self.first_name, self.last_name) if p)
        return full or None


def _usable_email(email: str | None) -> bool:
    return bool(email) and "@" in email and "not_unlocked" not in email and "domain.com" not in email


class Apollo:
    name = "apollo"
    base = "https://api.apollo.io/api/v1"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.exhausted = False

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and not self.exhausted

    def _post(self, path: str, payload: dict) -> dict:
        try:
            resp = httpx.post(
                f"{self.base}{path}",
                headers={"X-Api-Key": self.api_key, "Cache-Control": "no-cache"},
                json=payload,
                timeout=30,
            )
        except httpx.HTTPError as e:
            record_usage(self.name, ok=False, error=str(e))
            raise ProviderError(f"apollo network error: {e}") from e
        text = resp.text[:400]
        if resp.status_code in (401, 403, 429) or "insufficient credits" in text.lower():
            self.exhausted = True
            record_usage(self.name, ok=False, exhausted=True, error=f"{resp.status_code} {text}")
            raise ProviderExhausted(f"apollo {resp.status_code}: {text}")
        if resp.status_code >= 400:
            record_usage(self.name, ok=False, error=f"{resp.status_code} {text}")
            raise ProviderError(f"apollo {resp.status_code}: {text}")
        record_usage(self.name, ok=True)
        return resp.json()

    def search_people(self, domain: str) -> list[Candidate]:
        data = self._post("/mixed_people/api_search", {
            "q_organization_domains_list": [domain],
            "person_titles": TARGET_TITLES,
            "page": 1,
            "per_page": 10,
        })
        out = []
        for p in data.get("people", []):
            last = p.get("last_name")
            out.append(Candidate(
                source="apollo",
                first_name=p.get("first_name"),
                last_name=last if last and "*" not in last else None,  # search results obfuscate last names
                title=p.get("title"),
                linkedin_url=p.get("linkedin_url"),
                extra={"apollo_id": p.get("id"), "has_email": p.get("has_email")},
            ))
        return out

    def reveal(self, candidate: Candidate) -> Candidate:
        """Spends one Apollo credit to unlock the email (and full name)."""
        data = self._post("/people/match", {"id": candidate.extra.get("apollo_id"), "reveal_personal_emails": False})
        person = data.get("person") or {}
        candidate.first_name = person.get("first_name") or candidate.first_name
        candidate.last_name = person.get("last_name") or candidate.last_name
        candidate.linkedin_url = person.get("linkedin_url") or candidate.linkedin_url
        email = person.get("email")
        if _usable_email(email):
            candidate.email = email
            candidate.confidence = 90 if person.get("email_status") == "verified" else 70
        return candidate


_HIRING_INBOX_RE = re.compile(r"^(careers?|jobs?|hr|hiring|recruit\w*|talent\w*|people)([._-]|$)")


class Hunter:
    name = "hunter"
    base = "https://api.hunter.io/v2"

    def __init__(self, api_key: str, daily_left: int | None = None):
        self.api_key = api_key
        self.exhausted = False
        self.daily_left = daily_left  # None = no daily cap

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and not self.exhausted and (self.daily_left is None or self.daily_left > 0)

    @property
    def waiting_for_tomorrow(self) -> bool:
        """Credits are left this month, but today's share is spent."""
        return bool(self.api_key) and not self.exhausted and self.daily_left is not None and self.daily_left <= 0

    def _get(self, path: str, params: dict, *, track: bool = True) -> dict:
        try:
            resp = httpx.get(f"{self.base}{path}", params={**params, "api_key": self.api_key}, timeout=30)
        except httpx.HTTPError as e:
            if track:
                record_usage(self.name, ok=False, error=str(e))
            raise ProviderError(f"hunter network error: {e}") from e
        text = resp.text[:400]
        if resp.status_code in (401, 403, 429):
            self.exhausted = True
            if track:
                record_usage(self.name, ok=False, exhausted=True, error=f"{resp.status_code} {text}")
            raise ProviderExhausted(f"hunter {resp.status_code}: {text}")
        if resp.status_code >= 400:
            if track:
                record_usage(self.name, ok=False, error=f"{resp.status_code} {text}")
            raise ProviderError(f"hunter {resp.status_code}: {text}")
        if track:
            record_usage(self.name, ok=True)
        return resp.json().get("data", {})

    def domain_search(self, domain: str) -> tuple[list[Candidate], str | None]:
        """One search credit. Returns people with emails, hiring inboxes (careers@, hr@) and the company's email pattern."""
        if self.daily_left is not None:
            self.daily_left -= 1
        data = self._get("/domain-search", {"domain": domain, "limit": 10})
        out = []
        for e in data.get("emails", []):
            if not _usable_email(e.get("value")):
                continue
            if e.get("type") == "generic":
                if _HIRING_INBOX_RE.match(e["value"].split("@")[0].lower()):
                    out.append(Candidate(source="hunter", email=e["value"], title="careers inbox",
                                         confidence=e.get("confidence")))
                continue
            out.append(Candidate(
                source="hunter",
                email=e["value"],
                first_name=e.get("first_name"),
                last_name=e.get("last_name"),
                title=e.get("position") or e.get("department"),
                confidence=e.get("confidence"),
                linkedin_url=e.get("linkedin"),
                extra={"department": e.get("department")},
            ))
        return out, data.get("pattern")

    def remaining_searches(self) -> dict | None:
        """Free endpoint - doesn't use credits."""
        if not self.api_key:
            return None
        try:
            data = self._get("/account", {}, track=False)
        except (ProviderError, ProviderExhausted):
            return None
        searches = (data.get("requests") or {}).get("searches") or {}
        return {"used": searches.get("used"), "available": searches.get("available"), "reset_date": data.get("reset_date")}


def finders_from_settings() -> tuple[Apollo, Hunter]:
    s = get_settings()
    hunter_left = s.hunter_daily_searches - calls_today("hunter") if s.hunter_api_key else None
    return Apollo(s.apollo_api_key), Hunter(s.hunter_api_key, daily_left=hunter_left)
