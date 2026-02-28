import pytest
from fastapi.testclient import TestClient
from backend.main import app, init_app_db


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    import os
    os.environ["HIBID_DB_PATH"] = str(tmp_path / "test.db")
    init_app_db()


def test_health():
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_calculate_cost():
    client = TestClient(app)
    resp = client.post("/api/calculate", json={"bid_price": 100, "premium_pct": 15})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 129.95


def test_crud_auction_house():
    client = TestClient(app)
    resp = client.post("/api/auction-houses", json={"name": "Test House", "premium_pct": 15.0})
    assert resp.status_code == 200
    house_id = resp.json()["id"]
    resp = client.get("/api/auction-houses")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
    resp = client.delete(f"/api/auction-houses/{house_id}")
    assert resp.status_code == 200


def test_ebay_search():
    client = TestClient(app)
    resp = client.get("/api/search-ebay?query=Milwaukee+M18+Drill")
    assert resp.status_code == 200
    data = resp.json()
    assert "active" in data
    assert "sold" in data
    assert "amazon_url" in data
