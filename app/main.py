from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
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


class LocationPoint(BaseModel):
    id: UUID
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)
    recorded_at: datetime


class BatchPayload(BaseModel):
    client_id: UUID
    client_name: str = Field(min_length=1, max_length=80)
    points: list[LocationPoint] = Field(min_length=1, max_length=500)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Space Balloon Tracker",
    description="Offline-capable client GPS collection and route dashboard",
    lifespan=lifespan,
)
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
app.mount(
    "/client/위치",
    StaticFiles(directory=str(ROOT / "client" / "위치"), html=True),
    name="location-client",
)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"default_from": iso_utc(utc_now() - timedelta(hours=24)), "default_to": iso_utc(utc_now())},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/points/batch")
def receive_points(payload: BatchPayload):
    client_id = str(payload.client_id)
    client_name = payload.client_name.strip()
    if not client_name:
        raise HTTPException(status_code=422, detail="client_name cannot be blank")

    now = iso_utc(utc_now())
    inserted = 0
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO clients (id, name, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at""",
            (client_id, client_name, now, now),
        )
        for point in payload.points:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO points
                   (id, client_id, latitude, longitude, accuracy, recorded_at, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(point.id),
                    client_id,
                    point.latitude,
                    point.longitude,
                    point.accuracy,
                    iso_utc(point.recorded_at),
                    now,
                ),
            )
            inserted += cursor.rowcount
        total = conn.execute("SELECT COUNT(*) FROM points WHERE client_id = ?", (client_id,)).fetchone()[0]
    return {"client_id": client_id, "accepted": len(payload.points), "inserted": inserted, "total_points": total}


@app.get("/api/clients")
def list_clients():
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.created_at, c.updated_at,
                      COUNT(p.id) AS point_count, MAX(p.recorded_at) AS last_recorded_at
               FROM clients c LEFT JOIN points p ON p.client_id = c.id
               GROUP BY c.id ORDER BY c.name COLLATE NOCASE"""
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/points")
def list_points(
    client_id: Annotated[str | None, Query()] = None,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=20000)] = 10000,
):
    clauses: list[str] = []
    params: list[object] = []
    if client_id:
        try:
            client_id = str(UUID(client_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="client_id must be a UUID") from exc
        clauses.append("p.client_id = ?")
        params.append(client_id)
    if start:
        clauses.append("p.recorded_at >= ?")
        params.append(iso_utc(start))
    if end:
        clauses.append("p.recorded_at <= ?")
        params.append(iso_utc(end))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""SELECT p.id, p.client_id, c.name AS client_name, p.latitude, p.longitude,
                      p.accuracy, p.recorded_at, p.received_at
               FROM points p JOIN clients c ON c.id = p.client_id
               {where} ORDER BY p.recorded_at ASC LIMIT ?"""
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/summary")
def summary():
    with get_connection() as conn:
        clients = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        points = conn.execute("SELECT COUNT(*) FROM points").fetchone()[0]
    return {"clients": clients, "points": points}
