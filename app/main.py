from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "tracker.sqlite3"
DB_PATH = Path(os.environ.get("TRACKER_DB_PATH", DEFAULT_DB))


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS points (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                latitude REAL NOT NULL CHECK(latitude >= -90 AND latitude <= 90),
                longitude REAL NOT NULL CHECK(longitude >= -180 AND longitude <= 180),
                accuracy REAL,
                recorded_at TEXT NOT NULL,
                received_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_points_client_time
                ON points(client_id, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_points_recorded_at
                ON points(recorded_at);
            """
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class ClientUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class LocationCreate(BaseModel):
    id: UUID
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)
    recorded_at: datetime


class LocationBatch(BaseModel):
    locations: list[LocationCreate] = Field(min_length=1, max_length=500)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Space Balloon Tracker API",
    description=(
        "REST API for registering tracker clients and creating/querying GPS location resources. "
        "API documentation is available at /docs."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "default_from": iso_utc(utc_now() - timedelta(hours=24)),
            "default_to": iso_utc(utc_now()),
        },
    )


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}


def serialize_client(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "location_count": row["location_count"] if "location_count" in row.keys() else 0,
        "last_recorded_at": row["last_recorded_at"] if "last_recorded_at" in row.keys() else None,
    }


@app.put("/api/v1/clients/{client_id}", tags=["clients"])
def put_client(client_id: UUID, payload: ClientUpsert, response: Response):
    """Create or update a client; PUT is idempotent for a given client ID."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name cannot be blank")
    normalized_id = str(client_id)
    now = iso_utc(utc_now())
    with get_connection() as conn:
        existing = conn.execute("SELECT id FROM clients WHERE id = ?", (normalized_id,)).fetchone()
        conn.execute(
            """INSERT INTO clients (id, name, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at""",
            (normalized_id, name, now, now),
        )
        row = conn.execute(
            """SELECT c.id, c.name, c.created_at, c.updated_at,
                      COUNT(p.id) AS location_count, MAX(p.recorded_at) AS last_recorded_at
               FROM clients c LEFT JOIN points p ON p.client_id = c.id
               WHERE c.id = ? GROUP BY c.id""",
            (normalized_id,),
        ).fetchone()
    response.status_code = status.HTTP_200_OK if existing else status.HTTP_201_CREATED
    return serialize_client(row)


@app.get("/api/v1/clients", tags=["clients"])
def list_clients():
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.created_at, c.updated_at,
                      COUNT(p.id) AS location_count, MAX(p.recorded_at) AS last_recorded_at
               FROM clients c LEFT JOIN points p ON p.client_id = c.id
               GROUP BY c.id ORDER BY c.name COLLATE NOCASE"""
        ).fetchall()
    return [serialize_client(row) for row in rows]


@app.get("/api/v1/clients/{client_id}", tags=["clients"])
def get_client(client_id: UUID):
    with get_connection() as conn:
        row = conn.execute(
            """SELECT c.id, c.name, c.created_at, c.updated_at,
                      COUNT(p.id) AS location_count, MAX(p.recorded_at) AS last_recorded_at
               FROM clients c LEFT JOIN points p ON p.client_id = c.id
               WHERE c.id = ? GROUP BY c.id""",
            (str(client_id),),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return serialize_client(row)


def query_locations(
    *, client_id: str | None, start: datetime | None, end: datetime | None, limit: int
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if client_id:
        clauses.append("p.client_id = ?")
        params.append(client_id)
    if start:
        clauses.append("p.recorded_at >= ?")
        params.append(iso_utc(start))
    if end:
        clauses.append("p.recorded_at <= ?")
        params.append(iso_utc(end))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    statement = f"""SELECT p.id, p.client_id, c.name AS client_name, p.latitude, p.longitude,
                             p.accuracy, p.recorded_at, p.received_at
                      FROM points p JOIN clients c ON c.id = p.client_id
                      {where} ORDER BY p.recorded_at ASC, p.id ASC LIMIT ?"""
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(statement, params).fetchall()
    return [dict(row) for row in rows]


def check_time_range(start: datetime | None, end: datetime | None) -> None:
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="start must be earlier than or equal to end")


@app.post("/api/v1/clients/{client_id}/locations", tags=["locations"])
def create_locations(client_id: UUID, payload: LocationBatch, response: Response):
    """Create multiple location resources for a registered client (idempotent by location ID)."""
    normalized_id = str(client_id)
    now = iso_utc(utc_now())
    inserted = 0
    with get_connection() as conn:
        if conn.execute("SELECT 1 FROM clients WHERE id = ?", (normalized_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Client not found; register it with PUT /api/v1/clients/{client_id}")
        for location in payload.locations:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO points
                   (id, client_id, latitude, longitude, accuracy, recorded_at, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(location.id),
                    normalized_id,
                    location.latitude,
                    location.longitude,
                    location.accuracy,
                    iso_utc(location.recorded_at),
                    now,
                ),
            )
            inserted += cursor.rowcount
        total = conn.execute("SELECT COUNT(*) FROM points WHERE client_id = ?", (normalized_id,)).fetchone()[0]
    response.status_code = status.HTTP_201_CREATED if inserted else status.HTTP_200_OK
    return {
        "client_id": normalized_id,
        "accepted": len(payload.locations),
        "created": inserted,
        "total_locations": total,
    }


def location_query_parameters(
    client_id: str | None,
    start: datetime | None,
    end: datetime | None,
    limit: int,
):
    check_time_range(start, end)
    normalized_id = None
    if client_id:
        try:
            normalized_id = str(UUID(client_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="client_id must be a UUID") from exc
    return query_locations(client_id=normalized_id, start=start, end=end, limit=limit)


@app.get("/api/v1/locations", tags=["locations"])
def list_all_locations(
    client_id: Annotated[str | None, Query()] = None,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=20000)] = 10000,
):
    """List locations across clients with optional client/time filters."""
    return location_query_parameters(client_id, start, end, limit)


@app.get("/api/v1/clients/{client_id}/locations", tags=["locations"])
def list_client_locations(
    client_id: UUID,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=20000)] = 10000,
):
    """List location resources for a client, optionally filtered by time."""
    normalized_id = str(client_id)
    check_time_range(start, end)
    with get_connection() as conn:
        if conn.execute("SELECT 1 FROM clients WHERE id = ?", (normalized_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Client not found")
    return query_locations(client_id=normalized_id, start=start, end=end, limit=limit)


@app.get("/api/v1/locations/summary", tags=["locations"])
def location_summary():
    with get_connection() as conn:
        clients = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        locations = conn.execute("SELECT COUNT(*) FROM points").fetchone()[0]
    return {"clients": clients, "locations": locations}
