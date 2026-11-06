from fastapi.testclient import TestClient

from app.main import app


def test_health_reports_db_and_kill_switch() -> None:
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "database": "ok",
        "discord": "disabled",
        "agents_enabled": True,
    }
