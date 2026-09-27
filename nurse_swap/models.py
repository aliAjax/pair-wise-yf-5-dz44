"""领域模型：班次与换班单。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# ---- 换班单状态 ----
PENDING = "pending"        # 待处理：已提交，等待接替人接受
HANDOVER = "handover"      # 待交接：接替人已接受，等待双方依次确认
COMPLETED = "completed"    # 已完成：双方均已确认，班次已转交

# 未处理（进行中、占着名额）的换班：待处理 + 待交接
ACTIVE_STATUSES = (PENDING, HANDOVER)

# ---- 待交接阶段的确认顺序 ----
STAGE_AWAIT_ORIGINAL = "awaiting_original"  # 第一步：等原护士确认
STAGE_AWAIT_TARGET = "awaiting_target"      # 第二步：等接替人确认

STATUS_LABELS = {
    PENDING: "待处理",
    HANDOVER: "待交接",
    COMPLETED: "已完成",
}

STAGE_LABELS = {
    STAGE_AWAIT_ORIGINAL: "等待原护士确认",
    STAGE_AWAIT_TARGET: "等待接替人确认",
}


@dataclass
class Shift:
    """一个班次：归属护士、日期、起止时间（HH:MM，24 小时制）。"""

    id: int
    nurse: str
    date: str
    start_time: str
    end_time: str
    created_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "nurse": self.nurse,
            "date": self.date,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Shift":
        return cls(
            id=int(d["id"]),
            nurse=d["nurse"],
            date=d["date"],
            start_time=d["start_time"],
            end_time=d["end_time"],
            created_at=d["created_at"],
        )


@dataclass
class Swap:
    """一张换班单。

    生命周期：
      pending  --(接替人 accept)-->  handover/awaiting_original
      handover/awaiting_original --(原护士 confirm)--> handover/awaiting_target
      handover/awaiting_target  --(接替人 confirm)--> completed（班次转交）
      handover 且第二次确认之前 --(原护士 withdraw)--> handover/awaiting_original
          （原护士此前的确认作废，需要重新依次确认）
    """

    id: int
    shift_id: int
    original_nurse: str
    target_nurse: str
    status: str = PENDING
    confirm_stage: Optional[str] = None
    original_confirmed: bool = False
    target_confirmed: bool = False
    created_at: str = ""
    accepted_at: Optional[str] = None
    completed_at: Optional[str] = None

    def to_dict(self) -> dict:
        data = {
            "id": self.id,
            "shift_id": self.shift_id,
            "original_nurse": self.original_nurse,
            "target_nurse": self.target_nurse,
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "confirm_stage": self.confirm_stage,
            "confirm_stage_label": STAGE_LABELS.get(self.confirm_stage)
            if self.confirm_stage
            else None,
            "original_confirmed": self.original_confirmed,
            "target_confirmed": self.target_confirmed,
            "created_at": self.created_at,
            "accepted_at": self.accepted_at,
            "completed_at": self.completed_at,
        }
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "Swap":
        return cls(
            id=int(d["id"]),
            shift_id=int(d["shift_id"]),
            original_nurse=d["original_nurse"],
            target_nurse=d["target_nurse"],
            status=d["status"],
            confirm_stage=d.get("confirm_stage"),
            original_confirmed=bool(d.get("original_confirmed", False)),
            target_confirmed=bool(d.get("target_confirmed", False)),
            created_at=d.get("created_at", ""),
            accepted_at=d.get("accepted_at"),
            completed_at=d.get("completed_at"),
        )
