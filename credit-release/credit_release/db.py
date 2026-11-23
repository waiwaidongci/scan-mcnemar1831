"""SQLite 存储与表结构。所有写操作在同一把进程锁内串行化。"""

import json
import sqlite3
import threading

from .config import DB_PATH, ensure_dirs

_write_lock = threading.RLock()


def connect():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    role          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    role         TEXT NOT NULL CHECK (role IN ('tuning', 'acceptance')),
    batch_id     TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    filename     TEXT NOT NULL,
    checksum     TEXT NOT NULL,
    rows_json    TEXT NOT NULL,
    row_count    INTEGER NOT NULL,
    revealed     INTEGER NOT NULL DEFAULT 0,
    author       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_index (
    ticket_id  TEXT PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id)
);

CREATE TABLE IF NOT EXISTS threshold_sets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    version       INTEGER NOT NULL UNIQUE,
    status        TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    dataset_id    INTEGER NOT NULL REFERENCES datasets(id),
    dataset_checksum TEXT NOT NULL,
    caps_json     TEXT NOT NULL,
    sealed_json   TEXT NOT NULL,
    author        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    superseded_at TEXT
);

CREATE TABLE IF NOT EXISTS acceptance_batches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id        INTEGER NOT NULL UNIQUE REFERENCES datasets(id),
    threshold_set_id  INTEGER NOT NULL REFERENCES threshold_sets(id),
    bound_at          TEXT NOT NULL,
    bound_by          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acceptance_attempts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          INTEGER NOT NULL REFERENCES acceptance_batches(id),
    threshold_set_id  INTEGER NOT NULL REFERENCES threshold_sets(id),
    dataset_id        INTEGER NOT NULL REFERENCES datasets(id),
    dataset_checksum  TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('queued', 'running', 'complete', 'failed', 'interrupted')),
    attempt_no        INTEGER NOT NULL,
    resume_count      INTEGER NOT NULL DEFAULT 0,
    result_json       TEXT,
    error             TEXT,
    export_file       TEXT,
    publishable       INTEGER NOT NULL DEFAULT 0,
    lease_until       TEXT,
    author            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    finished_at       TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL,
    event      TEXT NOT NULL,
    target     TEXT,
    info_json  TEXT,
    created_at TEXT NOT NULL
);
"""


def init_db():
    with _write_lock, connect() as conn:
        conn.executescript(SCHEMA)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def loads(text):
    return json.loads(text) if text else None


class tx:  # noqa: N801 - 上下文管理器小工具
    def __enter__(self):
        _write_lock.acquire()
        self.conn = connect()
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
            _write_lock.release()


def read_conn():
    return connect()
