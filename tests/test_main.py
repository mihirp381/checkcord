from fastapi.testclient import TestClient

from checkcord.main import app


def test_healthz() -> None:
    """Test that the /healthz endpoint returns status ok."""
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
