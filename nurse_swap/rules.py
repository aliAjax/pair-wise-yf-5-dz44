"""纯业务规则：输入校验、时间/重叠/已开始/未处理换班判定。

本模块不碰存储、不碰 HTTP，只做无副作用的判定，方便单独测试。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from .models import ACTIVE_STATUSES, Shift, Swap

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class RuleValidationError(ValueError):
    """请求字段不合法（对应 HTTP 400）。"""


# ---------- 基础解析 ----------

def parse_date(value: str, field: str = "date") -> date:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise RuleValidationError(f"{field} 必须是 YYYY-MM-DD 格式")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RuleValidationError(f"{field} 不是合法日期") from exc


def parse_time(value: str, field: str) -> tuple[int, int]:
    if not isinstance(value, str) or not TIME_RE.match(value):
        raise RuleValidationError(f"{field} 必须是 HH:MM 24 小时制格式")
    hour, minute = int(value[0:2]), int(value[3:5])
    return hour, minute


def time_to_minutes(value: str) -> int:
    hour, minute = parse_time(value, "time")
    return hour * 60 + minute


def validate_nurse(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleValidationError(f"{field} 不能为空")
    return value.strip()


def validate_shift_fields(nurse, date_str, start_time, end_time) -> tuple[str, str, str, str]:
    """校验创建班次的字段，返回清洗后的 (nurse, date, start, end)。"""
    nurse = validate_nurse(nurse, "nurse")
    parse_date(date_str, "date")
    start_min = time_to_minutes(start_time) if start_time else -1
    end_min = time_to_minutes(end_time) if end_time else -1
    if not start_time or not end_time:
        raise RuleValidationError("start_time 和 end_time 必填")
    if start_min >= end_min:
        raise RuleValidationError("start_time 必须早于 end_time（不支持跨天班次）")
    return nurse, date_str, start_time, end_time


# ---------- 时刻计算 ----------

def shift_start_datetime(shift: Shift) -> datetime:
    d = date.fromisoformat(shift.date)
    h, m = int(shift.start_time[0:2]), int(shift.start_time[3:5])
    return datetime(d.year, d.month, d.day, h, m)


def shift_end_datetime(shift: Shift) -> datetime:
    d = date.fromisoformat(shift.date)
    h, m = int(shift.end_time[0:2]), int(shift.end_time[3:5])
    return datetime(d.year, d.month, d.day, h, m)


def is_shift_started(shift: Shift, now: datetime) -> bool:
    """原班次是否已开始（到点即算已开始）。"""
    return now >= shift_start_datetime(shift)


# ---------- 重叠判定 ----------

def _intervals_overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    """半开区间 [start, end) 是否重叠；首尾相接不算重叠。"""
    return start_a < end_b and start_b < end_a


def shifts_overlap(
    shift: Shift,
    date_str: str,
    start_time: str,
    end_time: str,
) -> bool:
    """同一自然日、时间段重叠才算重叠。"""
    if shift.date != date_str:
        return False
    return _intervals_overlap(
        time_to_minutes(shift.start_time),
        time_to_minutes(shift.end_time),
        time_to_minutes(start_time),
        time_to_minutes(end_time),
    )


def nurse_has_overlap(
    shifts: Iterable[Shift],
    nurse: str,
    date_str: str,
    start_time: str,
    end_time: str,
    exclude_shift_id: Optional[int] = None,
) -> bool:
    """该护士在同一日期是否已有时间重叠的班次。"""
    for shift in shifts:
        if shift.id == exclude_shift_id or shift.nurse != nurse:
            continue
        if shifts_overlap(shift, date_str, start_time, end_time):
            return True
    return False


# ---------- 换班单判定 ----------

def has_unresolved_swap(swaps: Iterable[Swap], shift_id: int) -> bool:
    """该班次是否存在未处理换班（待处理或待交接）。完成的不算。"""
    return any(
        swap.shift_id == shift_id and swap.status in ACTIVE_STATUSES
        for swap in swaps
    )
