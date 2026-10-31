from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app.db import engine, init_db
from app.main import app

H = {"X-API-Key": "test-key"}


def test_auth_and_endpoints():
    with TestClient(app) as c:
        assert c.get("/health").json() == {"ok": True}
        assert c.get("/stats").status_code == 401
        assert c.get("/stats", headers={"X-API-Key": "nope"}).status_code == 401
        for path in ["/stats", "/runs", "/drafts", "/jobs", "/jobs?status=no_contact", "/events?level=error", "/providers", "/linkedin"]:
            r = c.get(path, headers=H)
            assert r.status_code == 200, (path, r.text)
        assert c.get("/runs/999", headers=H).status_code == 404
        assert c.patch("/drafts/1", json={"status": "sent"}, headers=H).status_code == 404
        assert c.patch("/drafts/1", json={"status": "bogus"}, headers=H).status_code == 422
        assert c.patch("/linkedin/1", json={"status": "sent"}, headers=H).status_code == 404


def test_init_db_adds_columns_missing_from_an_older_database():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE jobs DROP COLUMN company_linkedin_url"))
    init_db()
    assert "company_linkedin_url" in {c["name"] for c in inspect(engine).get_columns("jobs")}
