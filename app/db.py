"""SQLite 连接与建表。

并发模型
--------
* WAL 模式：签发读操作与读数提交写操作不互相阻塞；
* 所有写事务以 ``BEGIN IMMEDIATE`` 开启，进入事务即获取 RESERVED 锁，
  读数提交（修订）与证书签发因此全局串行化。签发者在事务内读到的
  readings 一定是此前某个完整提交的状态，不会读到修订进行一半的数据
  —— 即“签发与修订并发时按同一已提交状态计算”。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import settings

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS windows (
    id                                   TEXT PRIMARY KEY,
    start_ts                             REAL NOT NULL,
    end_ts                               REAL NOT NULL,
    temp_min                             REAL NOT NULL,
    temp_max                             REAL NOT NULL,
    max_continuous_exceed_seconds        REAL NOT NULL,
    max_sampling_interval_seconds        REAL NOT NULL,
    created_at                           TEXT NOT NULL
);

-- 同一逻辑读数（reading_id）的每个修订号保留一行；superseded 标记是否已被
-- 更高修订号替换。只有 superseded = 0 的行参与审定计算与签发快照。
CREATE TABLE IF NOT EXISTS readings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id          TEXT NOT NULL REFERENCES windows(id),
    reading_id         TEXT NOT NULL,
    revision           INTEGER NOT NULL,
    ts                 REAL NOT NULL,
    temperature        REAL NOT NULL,
    superseded         INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    UNIQUE (window_id, reading_id, revision)
);

-- 每次签发新增一行（version 递增），snapshot 为不可变 JSON：
-- 已签发结论保留原快照，修订后再次签发形成新版本。
CREATE TABLE IF NOT EXISTS certificates (
    id          TEXT PRIMARY KEY,
    window_id   TEXT NOT NULL REFERENCES windows(id),
    version     INTEGER NOT NULL,
    verdict     TEXT NOT NULL,
    result      TEXT NOT NULL,
    issued_at   TEXT NOT NULL,
    UNIQUE (window_id, version)
);
"""


def connect(database_path: str | None = None) -> sqlite3.Connection:
    path = database_path or settings.database_path
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(database_path: str | None = None) -> None:
    conn = connect(database_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每个请求一个连接。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 写事务：提交修订与签发串行化的关键。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
