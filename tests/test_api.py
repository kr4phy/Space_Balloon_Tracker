from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture

def api(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.sqlite3")
    main.init_db()
    with TestClient(main.app) as client:
        yield client


def test_batch_insert_is_idempotent_and_registers_client(api):
    client_id = str(uuid4())
    point_id = str(uuid4())
    payload = {
        "client_id": client_id,
        "client_name": "열기구 A",
        "points": [{
            "id": point_id,
            "latitude": 37.5665,
            "longitude": 126.978,
            "accuracy": 8.5,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }],
    }

    first = api.post("/api/points/batch", json=payload)
    second = api.post("/api/points/batch", json=payload)

    assert first.status_code == 200
    assert first.json()["inserted"] == 1
    assert second.json()["inserted"] == 0
    assert second.json()["total_points"] == 1
    clients = api.get("/api/clients").json()
    assert clients[0]["name"] == "열기구 A"
    assert clients[0]["point_count"] == 1


def test_points_can_be_filtered_by_client_and_time(api):
    client_id = str(uuid4())
    now = datetime.now(timezone.utc)
    payload = {
        "client_id": client_id,
        "client_name": "풍선 B",
        "points": [
            {"id": str(uuid4()), "latitude": 37, "longitude": 127, "recorded_at": (now - timedelta(hours=2)).isoformat()},
            {"id": str(uuid4()), "latitude": 38, "longitude": 128, "recorded_at": now.isoformat()},
        ],
    }
    assert api.post("/api/points/batch", json=payload).status_code == 200

    response = api.get("/api/points", params={
        "client_id": client_id,
        "start": (now - timedelta(hours=1)).isoformat(),
    })
    assert response.status_code == 200
    points = response.json()
    assert len(points) == 1
    assert points[0]["client_name"] == "풍선 B"
    assert points[0]["latitude"] == 38


def test_invalid_coordinates_are_rejected(api):
    payload = {
        "client_id": str(uuid4()),
        "client_name": "잘못된 위치",
        "points": [{"id": str(uuid4()), "latitude": 95, "longitude": 0, "recorded_at": datetime.now(timezone.utc).isoformat()}],
    }
    assert api.post("/api/points/batch", json=payload).status_code == 422
