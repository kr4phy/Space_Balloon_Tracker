from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import main
from client import tracker


@pytest.fixture

def api(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.sqlite3")
    main.init_db()
    with TestClient(main.app) as client:
        yield client


def register(api, client_id, name="열기구 A"):
    response = api.put(f"/api/v1/clients/{client_id}", json={"name": name})
    assert response.status_code in (200, 201)
    return response


def test_rest_client_upsert_is_idempotent(api):
    client_id = str(uuid4())

    first = api.put(f"/api/v1/clients/{client_id}", json={"name": "열기구 A"})
    again = api.put(f"/api/v1/clients/{client_id}", json={"name": "열기구 A 수정"})

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["name"] == "열기구 A 수정"
    assert api.get("/api/v1/clients").json()[0]["id"] == client_id
    assert api.get(f"/api/v1/clients/{client_id}").json()["location_count"] == 0


def test_location_batch_is_idempotent_and_available_to_dashboard(api):
    client_id = str(uuid4())
    location_id = str(uuid4())
    register(api, client_id)
    payload = {
        "locations": [{
            "id": location_id,
            "latitude": 37.5665,
            "longitude": 126.978,
            "accuracy": 8.5,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }],
    }

    first = api.post(f"/api/v1/clients/{client_id}/locations", json=payload)
    again = api.post(f"/api/v1/clients/{client_id}/locations", json=payload)

    assert first.status_code == 201
    assert again.status_code == 200
    assert first.json()["created"] == 1
    assert again.json()["created"] == 0
    assert again.json()["total_locations"] == 1
    locations = api.get("/api/v1/locations").json()
    assert locations[0]["client_name"] == "열기구 A"
    assert locations[0]["latitude"] == 37.5665
    assert api.get("/api/v1/locations/summary").json() == {"clients": 1, "locations": 1}
    assert api.get("/api/v1/clients").json()[0]["location_count"] == 1


def test_client_and_time_filtered_location_resources(api):
    client_id = str(uuid4())
    other_id = str(uuid4())
    now = datetime.now(timezone.utc)
    register(api, client_id, "풍선 A")
    register(api, other_id, "풍선 B")
    payload = {
        "locations": [
            {"id": str(uuid4()), "latitude": 37, "longitude": 127, "recorded_at": (now - timedelta(hours=2)).isoformat()},
            {"id": str(uuid4()), "latitude": 38, "longitude": 128, "recorded_at": now.isoformat()},
        ],
    }
    api.post(f"/api/v1/clients/{client_id}/locations", json=payload)
    api.post(
        f"/api/v1/clients/{other_id}/locations",
        json={"locations": [{"id": str(uuid4()), "latitude": 39, "longitude": 129, "recorded_at": now.isoformat()}]},
    )

    response = api.get(f"/api/v1/clients/{client_id}/locations", params={"start": (now - timedelta(hours=1)).isoformat()})
    all_response = api.get("/api/v1/locations", params={"client_id": client_id, "end": now.isoformat()})
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["client_name"] == "풍선 A"
    assert len(all_response.json()) == 2
    assert api.get("/api/v1/clients/not-a-uuid").status_code == 422
    assert api.get(f"/api/v1/clients/{uuid4()}/locations").status_code == 404


def test_unknown_client_and_invalid_locations_are_rejected(api):
    missing_client = str(uuid4())
    post_response = api.post(
        f"/api/v1/clients/{missing_client}/locations",
        json={"locations": [{"id": str(uuid4()), "latitude": 37, "longitude": 127, "recorded_at": datetime.now(timezone.utc).isoformat()}]},
    )
    assert post_response.status_code == 404

    client_id = str(uuid4())
    register(api, client_id)
    invalid = api.post(
        f"/api/v1/clients/{client_id}/locations",
        json={"locations": [{"id": str(uuid4()), "latitude": 95, "longitude": 0, "recorded_at": datetime.now(timezone.utc).isoformat()}]},
    )
    assert invalid.status_code == 422
    assert api.get("/api/v1/locations", params={"start": "2026-10-19T00:00:00Z", "end": "2026-10-18T00:00:00Z"}).status_code == 422


def test_python_client_keeps_queue_until_rest_sync_succeeds(tmp_path, monkeypatch, api):
    queue = tracker.OfflineQueue(tmp_path / "client.sqlite3")
    location = queue.enqueue(37.5, 127.0, 4.0)
    # A new process opening the same file sees the same stable ID and queued point.
    reopened = tracker.OfflineQueue(tmp_path / "client.sqlite3")
    assert reopened.client_id == queue.client_id
    assert reopened.pending() == [location]

    calls = []
    def fake_api(server, method, path, payload=None, timeout=15):
        calls.append((method, path, payload))
        response = api.request(method, path, json=payload)
        return response.status_code, response.json()

    monkeypatch.setattr(tracker, "api_request", fake_api)
    assert tracker.sync_queue(reopened, "http://tracker.test", "테스트 클라이언트") == 1
    assert reopened.count() == 0
    assert calls[0][0:2] == ("PUT", f"/api/v1/clients/{queue.client_id}")
    assert calls[1][0:2] == ("POST", f"/api/v1/clients/{queue.client_id}/locations")
    assert calls[1][2]["locations"][0]["id"] == location["id"]
