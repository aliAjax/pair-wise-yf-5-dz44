"""存储层: SQLite 持久化。进程重启后数据仍在, 与规则层、HTTP 层完全解耦。"""
from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, time

from .errors import NotFoundError
from .models import Shift, SwapRequest, SwapState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shifts (
    id         TEXT PRIMARY KEY,
    nurse_id   TEXT NOT NULL,
    day        TEXT NOT NULL,  -- YYYY-MM-DD
    start_time TEXT NOT NULL,  -- HH:MM
    end_time   TEXT NOT NULL   -- HH:MM
);
CREATE TABLE IF NOT EXISTS swap_requests (
    id                  TEXT PRIMARY KEY,
    shift_id            TEXT NOT NULL REFERENCES shifts(id),
    requester_id        TEXT NOT NULL,
    acceptor_id         TEXT,
    state               TEXT NOT NULL,
    requester_confirmed INTEGER NOT NULL DEFAULT 0,
    acceptor_confirmed  INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_swap_shift     ON swap_requests(shift_id);
CREATE INDEX IF NOT EXISTS idx_swap_requester ON swap_requests(requester_id);
CREATE INDEX IF NOT EXISTS idx_swap_acceptor  ON swap_requests(acceptor_id);
"""

_OPEN_STATES = (SwapState.PENDING.value, SwapState.ACCEPTED.value)


class Store:
    """班次与换班申请的 SQLite 仓储(线程安全)。"""

    def __init__(self, db_path: str = "nurse_swap.db"):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- 班次 ----------------

    def add_shift(self, shift: Shift) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO shifts (id, nurse_id, day, start_time, end_time)"
                " VALUES (?, ?, ?, ?, ?)",
                (shift.id, shift.nurse_id, shift.day.isoformat(),
                 _fmt_time(shift.start_time), _fmt_time(shift.end_time)),
            )

    def update_shift(self, shift: Shift) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE shifts SET nurse_id = ?, day = ?, start_time = ?, end_time = ?"
                " WHERE id = ?",
                (shift.nurse_id, shift.day.isoformat(),
                 _fmt_time(shift.start_time), _fmt_time(shift.end_time), shift.id),
            )

    def find_shift(self, shift_id: str) -> Shift | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM shifts WHERE id = ?", (shift_id,)
            ).fetchone()
        return _row_to_shift(row) if row else None

    def get_shift(self, shift_id: str) -> Shift:
        shift = self.find_shift(shift_id)
        if shift is None:
            raise NotFoundError(f"班次不存在: {shift_id}")
        return shift

    def list_shifts_by_nurse(self, nurse_id: str) -> list[Shift]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM shifts WHERE nurse_id = ? ORDER BY day, start_time",
                (nurse_id,),
            ).fetchall()
        return [_row_to_shift(r) for r in rows]

    def list_shifts_by_nurse_on_day(self, nurse_id: str, day: date) -> list[Shift]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM shifts WHERE nurse_id = ? AND day = ?",
                (nurse_id, day.isoformat()),
            ).fetchall()
        return [_row_to_shift(r) for r in rows]

    # ---------------- 换班申请 ----------------

    def add_request(self, request: SwapRequest) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO swap_requests (id, shift_id, requester_id, acceptor_id,"
                " state, requester_confirmed, acceptor_confirmed, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request.id, request.shift_id, request.requester_id, request.acceptor_id,
                 request.state.value, int(request.requester_confirmed),
                 int(request.acceptor_confirmed),
                 request.created_at.isoformat(), request.updated_at.isoformat()),
            )

    def update_request(self, request: SwapRequest) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE swap_requests SET acceptor_id = ?, state = ?,"
                " requester_confirmed = ?, acceptor_confirmed = ?, updated_at = ?"
                " WHERE id = ?",
                (request.acceptor_id, request.state.value,
                 int(request.requester_confirmed), int(request.acceptor_confirmed),
                 request.updated_at.isoformat(), request.id),
            )

    def find_request(self, request_id: str) -> SwapRequest | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM swap_requests WHERE id = ?", (request_id,)
            ).fetchone()
        return _row_to_request(row) if row else None

    def get_request(self, request_id: str) -> SwapRequest:
        request = self.find_request(request_id)
        if request is None:
            raise NotFoundError(f"换班申请不存在: {request_id}")
        return request

    def find_open_request_for_shift(self, shift_id: str) -> SwapRequest | None:
        """该班次未处理(待处理/待交接)的换班申请, 没有则返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM swap_requests WHERE shift_id = ? AND state IN (?, ?)"
                " ORDER BY created_at LIMIT 1",
                (shift_id, *_OPEN_STATES),
            ).fetchone()
        return _row_to_request(row) if row else None

    def list_requests_by_nurse(
        self, nurse_id: str, state: SwapState | None = None
    ) -> list[SwapRequest]:
        """某护士相关的换班申请(作为原护士或接替人), 可按状态过滤。"""
        sql = ("SELECT * FROM swap_requests WHERE (requester_id = ? OR acceptor_id = ?)")
        params: list = [nurse_id, nurse_id]
        if state is not None:
            sql += " AND state = ?"
            params.append(state.value)
        sql += " ORDER BY created_at"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_request(r) for r in rows]


def _fmt_time(value: time) -> str:
    return value.strftime("%H:%M")


def _row_to_shift(row: sqlite3.Row) -> Shift:
    return Shift(
        id=row["id"],
        nurse_id=row["nurse_id"],
        day=date.fromisoformat(row["day"]),
        start_time=time.fromisoformat(row["start_time"]),
        end_time=time.fromisoformat(row["end_time"]),
    )


def _row_to_request(row: sqlite3.Row) -> SwapRequest:
    return SwapRequest(
        id=row["id"],
        shift_id=row["shift_id"],
        requester_id=row["requester_id"],
        acceptor_id=row["acceptor_id"],
        state=SwapState(row["state"]),
        requester_confirmed=bool(row["requester_confirmed"]),
        acceptor_confirmed=bool(row["acceptor_confirmed"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
