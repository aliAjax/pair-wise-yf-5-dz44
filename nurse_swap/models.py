"""领域模型: 班次(Shift)与换班申请(SwapRequest)。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum


class SwapState(str, Enum):
    """换班申请状态机。"""

    PENDING = "pending"      # 待处理: 已提交, 等待其他护士接替
    ACCEPTED = "accepted"    # 待交接: 已有人接替, 等待原护士与接替人依次确认
    COMPLETED = "completed"  # 已完成: 双方均确认, 班次归属已变更


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Shift:
    """班次: 某护士在某一天的起止时间(不跨天)。"""

    nurse_id: str
    day: date
    start_time: time
    end_time: time
    id: str = field(default_factory=_new_id)

    @property
    def start_at(self) -> datetime:
        return datetime.combine(self.day, self.start_time)

    @property
    def end_at(self) -> datetime:
        return datetime.combine(self.day, self.end_time)

    def has_started(self, now: datetime) -> bool:
        return self.start_at <= now

    def overlaps(self, other: "Shift") -> bool:
        """同一天且时间段相交(首尾相接不算重叠)。"""
        return (
            self.day == other.day
            and self.start_time < other.end_time
            and other.start_time < self.end_time
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "nurse_id": self.nurse_id,
            "date": self.day.isoformat(),
            "start_time": self.start_time.strftime("%H:%M"),
            "end_time": self.end_time.strftime("%H:%M"),
        }


@dataclass
class SwapRequest:
    """换班申请: 原护士(requester)发起, 接替人(acceptor)接替后双方依次确认。"""

    shift_id: str
    requester_id: str
    id: str = field(default_factory=_new_id)
    state: SwapState = SwapState.PENDING
    acceptor_id: str | None = None      # 接替人
    requester_confirmed: bool = False   # 第一次确认: 原护士
    acceptor_confirmed: bool = False    # 第二次确认: 接替人
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "shift_id": self.shift_id,
            "requester_id": self.requester_id,
            "acceptor_id": self.acceptor_id,
            "state": self.state.value,
            "requester_confirmed": self.requester_confirmed,
            "acceptor_confirmed": self.acceptor_confirmed,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
        }
