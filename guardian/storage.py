"""SQLite store; each call has a separate connection, safe across bot/worker threads."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import DATABASE, DEFAULT_SETTINGS


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path = DATABASE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ports (
                    port INTEGER PRIMARY KEY CHECK(port BETWEEN 1 AND 65535),
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS subscription_targets (
                    subscription_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    name TEXT NOT NULL, uri TEXT NOT NULL, port INTEGER NOT NULL,
                    PRIMARY KEY (subscription_id, position),
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL, finished_at TEXT,
                    state TEXT NOT NULL, total INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS results (
                    run_id INTEGER NOT NULL, sequence INTEGER NOT NULL,
                    name TEXT NOT NULL, port INTEGER NOT NULL,
                    status TEXT NOT NULL, detail TEXT NOT NULL, attempts INTEGER NOT NULL,
                    screenshot TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                );
            """)
            for key, value in DEFAULT_SETTINGS.items():
                db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (key, value))
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 30000")
        db.execute("PRAGMA journal_mode = WAL")
        return db

    def get(self, key: str) -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""

    def set(self, key: str, value: str):
        if key not in DEFAULT_SETTINGS:
            raise ValueError("Unknown setting")
        with self.connect() as db:
            db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, value))

    def settings(self) -> dict[str, str]:
        with self.connect() as db:
            return {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM settings")}

    def add_ports(self, ports: list[int]) -> int:
        with self.connect() as db:
            existing = {r[0] for r in db.execute("SELECT port FROM ports")}
            if len(existing.union(ports)) > 1000:
                raise ValueError("حداکثر ۱۰۰۰ پورت در مجموع مجاز است.")
            before = db.total_changes
            db.executemany("INSERT OR IGNORE INTO ports(port,enabled) VALUES (?,1)", [(p,) for p in ports])
            return db.total_changes - before

    def list_ports(self) -> list[tuple[int, bool]]:
        with self.connect() as db:
            return [(r["port"], bool(r["enabled"])) for r in db.execute("SELECT port,enabled FROM ports ORDER BY port")]

    def remove_port(self, port: int):
        with self.connect() as db:
            db.execute("DELETE FROM ports WHERE port=?", (port,))

    def toggle_port(self, port: int):
        with self.connect() as db:
            db.execute("UPDATE ports SET enabled=1-enabled WHERE port=?", (port,))

    def replace_ports(self, ports: list[int]):
        with self.connect() as db:
            db.execute("DELETE FROM ports")
            db.executemany("INSERT INTO ports(port,enabled) VALUES (?,1)", [(p,) for p in ports])

    def add_subscription(self, url: str):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO subscriptions(url) VALUES (?)", (url,))

    def list_subscriptions(self) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT s.id,s.url,s.enabled,s.updated_at,COUNT(t.position) AS nodes "
                "FROM subscriptions s LEFT JOIN subscription_targets t ON t.subscription_id=s.id "
                "GROUP BY s.id ORDER BY s.id")]

    def remove_subscription(self, sub_id: int):
        with self.connect() as db:
            db.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))

    def save_subscription_targets(self, sub_id: int, targets):
        with self.connect() as db:
            db.execute("DELETE FROM subscription_targets WHERE subscription_id=?", (sub_id,))
            db.executemany(
                "INSERT INTO subscription_targets(subscription_id,position,name,uri,port) VALUES (?,?,?,?,?)",
                [(sub_id, idx, t.name, t.uri, t.port) for idx, t in enumerate(targets, 1)],
            )
            db.execute("UPDATE subscriptions SET updated_at=? WHERE id=?", (utc_now(), sub_id))

    def cached_targets(self):
        from .sources import Target
        with self.connect() as db:
            rows = db.execute(
                "SELECT t.name,t.uri,t.port FROM subscription_targets t "
                "JOIN subscriptions s ON s.id=t.subscription_id WHERE s.enabled=1 "
                "ORDER BY t.subscription_id,t.position"
            ).fetchall()
        return [Target(r["name"], r["uri"], r["port"]) for r in rows]

    def begin_run(self, count: int) -> int:
        with self.connect() as db:
            cursor = db.execute("INSERT INTO runs(started_at,state,total) VALUES (?,?,?)",
                                (utc_now(), "running", count))
            return cursor.lastrowid

    def add_result(self, run_id: int, idx: int, name: str, port: int,
                   status: str, detail: str, attempts: int, screenshot: str):
        with self.connect() as db:
            db.execute(
                "INSERT INTO results(run_id,sequence,name,port,status,detail,attempts,screenshot,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, idx, name, port, status, detail, attempts, screenshot, utc_now()),
            )
            db.execute("UPDATE runs SET completed=completed+1 WHERE id=?", (run_id,))

    def finish_run(self, run_id: int, state: str, note: str = ""):
        with self.connect() as db:
            db.execute("UPDATE runs SET finished_at=?, state=?, note=? WHERE id=?",
                       (utc_now(), state, note[:500], run_id))

    def latest_run(self):
        with self.connect() as db:
            r = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            return dict(r) if r else None

    def run(self, run_id: int):
        with self.connect() as db:
            r = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return dict(r) if r else None

    def history(self, limit: int = 10):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]

    def results(self, run_id: int, limit: int = 1000):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM results WHERE run_id=? ORDER BY sequence LIMIT ?",
                (run_id, limit))]
