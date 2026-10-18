from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

DEFAULT_SERVER = os.environ.get("TRACKER_SERVER", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_QUEUE = Path(os.environ.get("TRACKER_CLIENT_DB", Path.home() / ".space-balloon-tracker" / "queue.sqlite3"))
BATCH_SIZE = 200


def utc_iso(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class OfflineQueue:
    """Durable local SQLite outbox; queued points survive process and network outages."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pending_locations (
                    id TEXT PRIMARY KEY,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    accuracy REAL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pending_recorded ON pending_locations(recorded_at);
                """
            )
            if conn.execute("SELECT 1 FROM settings WHERE key = 'client_id'").fetchone() is None:
                conn.execute("INSERT INTO settings (key, value) VALUES ('client_id', ?)", (str(uuid4()),))

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    @property
    def client_id(self) -> str:
        with self.connect() as conn:
            return conn.execute("SELECT value FROM settings WHERE key = 'client_id'").fetchone()["value"]

    def enqueue(self, latitude: float, longitude: float, accuracy: float | None = None,
                recorded_at: datetime | None = None) -> dict:
        if not -90 <= latitude <= 90:
            raise ValueError("latitude must be in [-90, 90]")
        if not -180 <= longitude <= 180:
            raise ValueError("longitude must be in [-180, 180]")
        if accuracy is not None and accuracy < 0:
            raise ValueError("accuracy cannot be negative")
        location = {
            "id": str(uuid4()),
            "latitude": latitude,
            "longitude": longitude,
            "accuracy": accuracy,
            "recorded_at": utc_iso(recorded_at),
        }
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO pending_locations (id, latitude, longitude, accuracy, recorded_at)
                   VALUES (:id, :latitude, :longitude, :accuracy, :recorded_at)""",
                location,
            )
        return location

    def pending(self, limit: int = BATCH_SIZE) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT id, latitude, longitude, accuracy, recorded_at
                   FROM pending_locations ORDER BY recorded_at, id LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def remove(self, location_ids: list[str]) -> None:
        if not location_ids:
            return
        with self.connect() as conn:
            conn.executemany("DELETE FROM pending_locations WHERE id = ?", ((item,) for item in location_ids))

    def count(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM pending_locations").fetchone()[0]


def api_request(server: str, method: str, path: str, payload: dict | None = None, timeout: float = 15) -> tuple[int, dict | list | None]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{server.rstrip('/')}{path}",
        data=body,
        method=method,
        headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {})},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read()
            return response.status, json.loads(data) if data else None
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API returned HTTP {exc.code}: {message}") from exc
    except (URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
        raise ConnectionError(f"Cannot reach API at {server}: {exc}") from exc


def register_client(server: str, client_id: str, name: str) -> dict:
    _, client = api_request(server, "PUT", f"/api/v1/clients/{client_id}", {"name": name})
    if not isinstance(client, dict):
        raise RuntimeError("Server returned an invalid client response")
    return client


def sync_queue(queue: OfflineQueue, server: str, name: str) -> int:
    client = register_client(server, queue.client_id, name)
    print(f"Registered client {client['name']} ({client['id']})")
    synced = 0
    while batch := queue.pending():
        _, result = api_request(
            server,
            "POST",
            f"/api/v1/clients/{queue.client_id}/locations",
            {"locations": batch},
        )
        queue.remove([location["id"] for location in batch])
        synced += len(batch)
        print(f"Synced {synced} location(s); server created {result['created']} new record(s).")
    if not synced:
        print("No pending locations to sync.")
    return synced


def read_gpsd_fix(host: str, port: int, timeout: float) -> tuple[float, float, float | None, datetime]:
    """Connect to gpsd JSON streaming protocol and return the next valid TPV fix."""
    deadline = time.monotonic() + timeout
    with socket.create_connection((host, port), timeout=min(timeout, 10)) as gpsd:
        gpsd.settimeout(min(timeout, 10))
        gpsd.sendall(b'?WATCH={"enable":true,"json":true};\n')
        buffered = b""
        while time.monotonic() < deadline:
            try:
                chunk = gpsd.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("gpsd closed the connection")
            buffered += chunk
            while b"\n" in buffered:
                raw, buffered = buffered.split(b"\n", 1)
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if message.get("class") != "TPV" or message.get("mode", 0) < 2:
                    continue
                if not isinstance(message.get("lat"), (int, float)) or not isinstance(message.get("lon"), (int, float)):
                    continue
                recorded = datetime.fromisoformat(message["time"].replace("Z", "+00:00")) if message.get("time") else datetime.now(timezone.utc)
                accuracy = message.get("epx")
                epy = message.get("epy")
                if isinstance(accuracy, (int, float)) and isinstance(epy, (int, float)):
                    accuracy = max(float(accuracy), float(epy))
                elif not isinstance(accuracy, (int, float)):
                    accuracy = None
                return float(message["lat"]), float(message["lon"]), accuracy, recorded
    raise TimeoutError(f"No valid GPS fix received from gpsd within {timeout:g} seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect GPS fixes in a local SQLite outbox and sync them to the tracker REST API.")
    parser.add_argument("--server", default=DEFAULT_SERVER, help=f"API server base URL (default: {DEFAULT_SERVER})")
    parser.add_argument("--name", required=True, help="Human-readable client name shown on the dashboard")
    parser.add_argument("--queue-db", type=Path, default=DEFAULT_QUEUE, help=f"Offline queue SQLite file (default: {DEFAULT_QUEUE})")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Continuously read GPS fixes from gpsd and sync when online")
    run.add_argument("--interval", type=float, default=60, help="Seconds between GPS samples (default: 60)")
    run.add_argument("--gpsd-host", default="127.0.0.1", help="gpsd hostname (default: 127.0.0.1)")
    run.add_argument("--gpsd-port", type=int, default=2947, help="gpsd TCP port (default: 2947)")
    run.add_argument("--gps-timeout", type=float, default=20, help="Seconds to wait for a GPS fix per sample")

    subparsers.add_parser("sync", help="Sync the saved offline queue now")

    add = subparsers.add_parser("add", help="Manually enqueue a GPS coordinate (useful for devices without gpsd)")
    add.add_argument("--latitude", type=float, required=True)
    add.add_argument("--longitude", type=float, required=True)
    add.add_argument("--accuracy", type=float)
    add.add_argument("--recorded-at", help="ISO-8601 time (defaults to current UTC time)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    name = args.name.strip()
    if not name:
        print("Client name cannot be blank.", file=sys.stderr)
        return 2
    queue = OfflineQueue(args.queue_db)
    print(f"Client: {name} ({queue.client_id})")
    print(f"Pending local locations: {queue.count()}")

    if args.command == "add":
        recorded = datetime.fromisoformat(args.recorded_at.replace("Z", "+00:00")) if args.recorded_at else None
        location = queue.enqueue(args.latitude, args.longitude, args.accuracy, recorded)
        print(f"Queued location {location['id']} at {location['latitude']}, {location['longitude']}.")
        try:
            sync_queue(queue, args.server, name)
        except (ConnectionError, RuntimeError) as exc:
            print(f"Not synced yet; kept safely in local queue: {exc}", file=sys.stderr)
        return 0

    if args.command == "sync":
        try:
            sync_queue(queue, args.server, name)
            return 0
        except (ConnectionError, RuntimeError) as exc:
            print(f"Sync failed; queued locations remain on disk: {exc}", file=sys.stderr)
            return 1

    if args.interval <= 0 or args.gps_timeout <= 0:
        print("Interval and GPS timeout must be greater than zero.", file=sys.stderr)
        return 2
    print(f"Collecting from gpsd at {args.gpsd_host}:{args.gpsd_port} every {args.interval:g}s. Press Ctrl+C to stop.")
    try:
        while True:
            started = time.monotonic()
            try:
                latitude, longitude, accuracy, recorded_at = read_gpsd_fix(args.gpsd_host, args.gpsd_port, args.gps_timeout)
                location = queue.enqueue(latitude, longitude, accuracy, recorded_at)
                print(f"[{location['recorded_at']}] queued {latitude:.6f}, {longitude:.6f} accuracy={accuracy}")
            except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                print(f"GPS sample unavailable; existing queue retained: {exc}", file=sys.stderr)
            if queue.count():
                try:
                    sync_queue(queue, args.server, name)
                except (ConnectionError, RuntimeError) as exc:
                    print(f"Offline/API error; {queue.count()} point(s) remain queued: {exc}", file=sys.stderr)
            remaining = max(0, args.interval - (time.monotonic() - started))
            time.sleep(remaining)
    except KeyboardInterrupt:
        print(f"\nStopped. {queue.count()} unsynced point(s) remain in {queue.path}.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
