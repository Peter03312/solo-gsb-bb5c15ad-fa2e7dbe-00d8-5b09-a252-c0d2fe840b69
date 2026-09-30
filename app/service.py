"""业务逻辑层：窗口、读数提交（幂等 / 修订 / 拒收）、审定计算、签发版本。"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone

from . import db as db_mod
from .evaluation import EPS, Reading as ReadingPoint, WindowSpec, evaluate
from .schemas import ReadingSubmit, WindowCreate
from .timeutil import epoch_to_datetime, to_epoch


class DomainError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _window_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "start": epoch_to_datetime(row["start_ts"]),
        "end": epoch_to_datetime(row["end_ts"]),
        "temp_min": row["temp_min"],
        "temp_max": row["temp_max"],
        "max_continuous_exceed_seconds": row["max_continuous_exceed_seconds"],
        "max_sampling_interval_seconds": row["max_sampling_interval_seconds"],
        "created_at": datetime.fromisoformat(row["created_at"]),
    }


def _reading_dict(row: sqlite3.Row) -> dict:
    return {
        "reading_id": row["reading_id"],
        "revision": row["revision"],
        "timestamp": epoch_to_datetime(row["ts"]),
        "temperature": row["temperature"],
        "superseded": bool(row["superseded"]),
    }


def _get_window_row(conn: sqlite3.Connection, window_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM windows WHERE id = ?", (window_id,)).fetchone()
    if row is None:
        raise DomainError(404, "not_found", f"窗口 {window_id} 不存在")
    return row


def _window_spec(row: sqlite3.Row) -> WindowSpec:
    return WindowSpec(
        start=row["start_ts"],
        end=row["end_ts"],
        temp_min=row["temp_min"],
        temp_max=row["temp_max"],
        max_continuous_exceed_seconds=row["max_continuous_exceed_seconds"],
        max_sampling_interval_seconds=row["max_sampling_interval_seconds"],
    )


def _active_reading_rows(conn: sqlite3.Connection, window_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM readings WHERE window_id = ? AND superseded = 0 ORDER BY ts",
        (window_id,),
    ).fetchall()


def _evaluation_payload(window_row: sqlite3.Row, reading_rows: list[sqlite3.Row]) -> dict:
    result = evaluate(
        _window_spec(window_row),
        [
            ReadingPoint(
                reading_id=r["reading_id"],
                revision=r["revision"],
                ts=r["ts"],
                temperature=r["temperature"],
            )
            for r in reading_rows
        ],
    )
    return {
        "window_id": window_row["id"],
        "verdict": result["verdict"],
        "segments": [
            {
                "start": epoch_to_datetime(seg["start"]),
                "end": epoch_to_datetime(seg["end"]),
                "duration_seconds": seg["duration_seconds"],
                "status": seg["status"],
                "temperatures": seg["temperatures"],
            }
            for seg in result["segments"]
        ],
        "normal_seconds": result["normal_seconds"],
        "exceeded_seconds": result["exceeded_seconds"],
        "unknown_seconds": result["unknown_seconds"],
        "window_seconds": result["window_seconds"],
        "longest_continuous_exceeded_seconds": result["longest_continuous_exceeded_seconds"],
    }


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------

def create_window(conn: sqlite3.Connection, payload: WindowCreate) -> dict:
    start = to_epoch(payload.start)
    end = to_epoch(payload.end)
    if not (end - start > EPS):
        raise DomainError(422, "invalid_window", "窗口结束时间必须晚于起始时间")
    if payload.temp_min > payload.temp_max + EPS:
        raise DomainError(422, "invalid_window", "温度下限不能高于上限")

    window_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO windows
           (id, start_ts, end_ts, temp_min, temp_max,
            max_continuous_exceed_seconds, max_sampling_interval_seconds, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            window_id, start, end, payload.temp_min, payload.temp_max,
            payload.max_continuous_exceed_seconds,
            payload.max_sampling_interval_seconds, _now_iso(),
        ),
    )
    conn.commit()
    return _window_dict(_get_window_row(conn, window_id))


def get_window(conn: sqlite3.Connection, window_id: str) -> dict:
    return _window_dict(_get_window_row(conn, window_id))


def list_windows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM windows ORDER BY start_ts, id").fetchall()
    return [_window_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 读数提交
# ---------------------------------------------------------------------------

def submit_reading(conn: sqlite3.Connection, window_id: str, payload: ReadingSubmit) -> dict:
    # 整个检查 + 替换在 IMMEDIATE 事务内完成，避免并发修订互相覆盖。
    with db_mod.write_transaction(conn):
        window = _get_window_row(conn, window_id)
        ts = to_epoch(payload.timestamp)
        temp = payload.temperature
        if not math.isfinite(temp):
            raise DomainError(422, "invalid_reading", "温度必须为有限数值")

        spec = _window_spec(window)
        if ts < spec.start - EPS or ts > spec.end + EPS:
            raise DomainError(
                422, "outside_window", "读数时间戳在审定窗口之外",
                details={
                    "timestamp": payload.timestamp.isoformat(),
                    "window_start": epoch_to_datetime(spec.start).isoformat(),
                    "window_end": epoch_to_datetime(spec.end).isoformat(),
                },
            )

        same_id = conn.execute(
            "SELECT * FROM readings WHERE window_id = ? AND reading_id = ? AND superseded = 0",
            (window_id, payload.reading_id),
        ).fetchone()

        if same_id is not None:
            if payload.revision < same_id["revision"]:
                raise DomainError(
                    409, "stale_revision",
                    f"修订号 {payload.revision} 已过期，当前生效修订号为 {same_id['revision']}（旧修订号拒绝）",
                    details={"current_revision": same_id["revision"]},
                )
            if payload.revision == same_id["revision"]:
                same_time = abs(same_id["ts"] - ts) <= EPS
                same_temp = same_id["temperature"] == temp
                if same_time and same_temp:
                    # 相同 ID + 修订号 + 相同内容：幂等返回，不重复落库。
                    return {"status": "idempotent", "reading": _reading_dict(same_id),
                            "replaced_revision": None}
                raise DomainError(
                    409, "conflicting_revision",
                    "相同 ID 与修订号的读数内容冲突（时间戳或温度不一致）",
                    details={
                        "existing_timestamp": epoch_to_datetime(same_id["ts"]).isoformat(),
                        "existing_temperature": same_id["temperature"],
                    },
                )

        # 同一窗口内不同读数不得落在同一时刻。
        clash = conn.execute(
            """SELECT * FROM readings
               WHERE window_id = ? AND superseded = 0 AND reading_id != ?
                 AND ABS(ts - ?) <= ?""",
            (window_id, payload.reading_id, ts, EPS),
        ).fetchone()
        if clash is not None:
            raise DomainError(
                422, "duplicate_timestamp",
                f"时间戳与读数 {clash['reading_id']}（修订 {clash['revision']}）重合，同刻不同读数拒收",
                details={"conflicting_reading_id": clash["reading_id"]},
            )

        replaced_revision = None
        if same_id is not None:  # 更高修订号：旧行作废后重算将基于新行
            conn.execute(
                "UPDATE readings SET superseded = 1 WHERE id = ?", (same_id["id"],)
            )
            replaced_revision = same_id["revision"]

        cur = conn.execute(
            """INSERT INTO readings
               (window_id, reading_id, revision, ts, temperature, superseded, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (window_id, payload.reading_id, payload.revision, ts, temp, _now_iso()),
        )
        row = conn.execute("SELECT * FROM readings WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {
            "status": "replaced" if replaced_revision is not None else "accepted",
            "reading": _reading_dict(row),
            "replaced_revision": replaced_revision,
        }


def list_readings(conn: sqlite3.Connection, window_id: str, include_superseded: bool) -> list[dict]:
    _get_window_row(conn, window_id)
    if include_superseded:
        rows = conn.execute(
            "SELECT * FROM readings WHERE window_id = ? ORDER BY ts, revision",
            (window_id,),
        ).fetchall()
    else:
        rows = _active_reading_rows(conn, window_id)
    return [_reading_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 审定与签发
# ---------------------------------------------------------------------------

def evaluate_window(conn: sqlite3.Connection, window_id: str) -> dict:
    window = _get_window_row(conn, window_id)
    return _evaluation_payload(window, _active_reading_rows(conn, window_id))


def _certificate_dict(row: sqlite3.Row) -> dict:
    snapshot = json.loads(row["result"])
    # 快照经 JSON 往返后时间戳为字符串，还原为 datetime 与其他服务层输出保持一致。
    for seg in snapshot["evaluation"]["segments"]:
        seg["start"] = datetime.fromisoformat(seg["start"])
        seg["end"] = datetime.fromisoformat(seg["end"])
    for r in snapshot["readings"]:
        r["timestamp"] = datetime.fromisoformat(r["timestamp"])
        r["superseded"] = bool(r["superseded"])
    return {
        "id": row["id"],
        "window_id": row["window_id"],
        "version": row["version"],
        "verdict": row["verdict"],
        "issued_at": datetime.fromisoformat(row["issued_at"]),
        "result": snapshot["evaluation"],
        "readings": snapshot["readings"],
    }


def issue_certificate(conn: sqlite3.Connection, window_id: str) -> dict:
    with db_mod.write_transaction(conn):
        window = _get_window_row(conn, window_id)
        reading_rows = _active_reading_rows(conn, window_id)
        evaluation = _evaluation_payload(window, reading_rows)

        # 在同一已提交快照上计算并落库；与修订互斥（IMMEDIATE 锁）。
        latest = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM certificates WHERE window_id = ?",
            (window_id,),
        ).fetchone()["v"]
        version = latest + 1

        readings = [_reading_dict(r) for r in reading_rows]
        snapshot = {"evaluation": evaluation, "readings": readings}
        cert_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO certificates (id, window_id, version, verdict, result, issued_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                cert_id, window_id, version, evaluation["verdict"],
                json.dumps(snapshot, default=lambda o: o.isoformat() if isinstance(o, datetime) else o),
                _now_iso(),
            ),
        )
        row = conn.execute("SELECT * FROM certificates WHERE id = ?", (cert_id,)).fetchone()
        return _certificate_dict(row)


def list_certificates(conn: sqlite3.Connection, window_id: str) -> list[dict]:
    _get_window_row(conn, window_id)
    rows = conn.execute(
        "SELECT * FROM certificates WHERE window_id = ? ORDER BY version", (window_id,)
    ).fetchall()
    return [_certificate_dict(r) for r in rows]


def get_certificate(conn: sqlite3.Connection, window_id: str, version: int) -> dict:
    _get_window_row(conn, window_id)
    row = conn.execute(
        "SELECT * FROM certificates WHERE window_id = ? AND version = ?",
        (window_id, version),
    ).fetchone()
    if row is None:
        raise DomainError(404, "not_found", f"窗口 {window_id} 不存在第 {version} 版证书")
    return _certificate_dict(row)
