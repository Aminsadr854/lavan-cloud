from fastapi.testclient import TestClient

from lavan.api import app


def test_health():
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
