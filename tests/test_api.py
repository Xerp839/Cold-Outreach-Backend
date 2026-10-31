from fastapi.testclient import TestClient

from app.main import app

H = {"X-API-Key": "test-key"}


def test_auth_and_endpoints():
    with TestClient(app) as c:
        assert c.get("/health").json() == {"ok": True}
        assert c.get("/stats").status_code == 401
        assert c.get("/stats", headers={"X-API-Key": "nope"}).status_code == 401
        for path in ["/stats", "/runs", "/drafts", "/jobs", "/jobs?status=no_contact", "/events?level=error", "/providers"]:
            r = c.get(path, headers=H)
            assert r.status_code == 200, (path, r.text)
        assert c.get("/runs/999", headers=H).status_code == 404
        assert c.patch("/drafts/1", json={"status": "sent"}, headers=H).status_code == 404
        assert c.patch("/drafts/1", json={"status": "bogus"}, headers=H).status_code == 422
