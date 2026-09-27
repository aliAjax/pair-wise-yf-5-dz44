"""业务规则层: 纯函数校验, 不依赖存储与 HTTP, 违反规则时抛出 RuleViolation。"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .errors import RuleViolation
from .models import Shift, SwapRequest, SwapState


def ensure_requester_owns_shift(shift: Shift, requester_id: str) -> None:
    """只能为自己的班次提交换班。"""
    if shift.nurse_id != requester_id:
        raise RuleViolation(f"班次 {shift.id} 不属于护士 {requester_id}, 不能提交换班")


def ensure_shift_not_started(shift: Shift, now: datetime) -> None:
    """原班次已开始则拒绝。"""
    if shift.has_started(now):
        raise RuleViolation(f"班次 {shift.id} 已开始, 不能换班")


def ensure_no_open_request(open_request: SwapRequest | None) -> None:
    """同一班次已有未处理(待处理/待交接)的换班申请则拒绝。"""
    if open_request is not None:
        raise RuleViolation(f"该班次已有未处理的换班申请 {open_request.id}")


def ensure_acceptable(
    request: SwapRequest,
    shift: Shift,
    acceptor_id: str,
    acceptor_shifts_same_day: Iterable[Shift],
    now: datetime,
) -> None:
    """接替校验: 待处理状态、不能接替自己、原班次未开始、接替人当日无重叠班次。"""
    if request.state is not SwapState.PENDING:
        raise RuleViolation(f"换班申请 {request.id} 当前不在待处理状态, 无法接替")
    if acceptor_id == request.requester_id:
        raise RuleViolation("不能接替自己的班次")
    ensure_shift_not_started(shift, now)
    for other in acceptor_shifts_same_day:
        if shift.overlaps(other):
            raise RuleViolation(
                f"接替人 {acceptor_id} 当日班次 {other.id} "
                f"({other.start_time:%H:%M}-{other.end_time:%H:%M}) 与目标班次时间重叠"
            )


def ensure_confirm_turn(request: SwapRequest, nurse_id: str) -> str:
    """依次确认: 先原护士, 后接替人。返回本次确认的角色("requester"/"acceptor")。"""
    if request.state is not SwapState.ACCEPTED:
        raise RuleViolation(f"换班申请 {request.id} 当前不在待交接状态, 无法确认")
    if not request.requester_confirmed:
        if nurse_id != request.requester_id:
            raise RuleViolation("需由原护士先确认")
        return "requester"
    if nurse_id != request.acceptor_id:
        raise RuleViolation("原护士已确认, 需由接替人确认")
    return "acceptor"


def ensure_withdrawable(request: SwapRequest, nurse_id: str) -> None:
    """第二次确认前(即完成前)原护士可撤回; 撤回后回到待处理, 此前的确认作废。"""
    if request.state is SwapState.COMPLETED:
        raise RuleViolation("换班已完成, 不能撤回")
    if request.state is not SwapState.ACCEPTED:
        raise RuleViolation("换班申请尚未被接替, 无需撤回")
    if nurse_id != request.requester_id:
        raise RuleViolation("只有原护士可以撤回")
