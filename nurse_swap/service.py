"""业务编排层：把规则（rules）和存储（storage）组合成用例。

所有会改变状态的操作都在同一把 RLock 内完成，保证检查与写入的原子性。
时间通过 clock 注入，测试可固定时钟；默认使用系统当前时间。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, Optional

from .errors import ApiError
from .models import (
    COMPLETED,
    HANDOVER,
    PENDING,
    STAGE_AWAIT_ORIGINAL,
    STAGE_AWAIT_TARGET,
    Shift,
    Swap,
)
from .rules import (
    RuleValidationError,
    has_unresolved_swap,
    is_shift_started,
    nurse_has_overlap,
    validate_nurse,
    validate_shift_fields,
)
from .storage import JsonFileStorage


def _default_clock() -> datetime:
    return datetime.now()


class NurseSwapService:
    def __init__(
        self,
        storage: JsonFileStorage,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.storage = storage
        self.clock = clock or _default_clock
        self._lock = threading.RLock()

    def now(self) -> datetime:
        return self.clock()

    def _now_iso(self) -> str:
        return self.now().replace(microsecond=0).isoformat()

    # ================= 班次 =================

    def create_shift(self, nurse, date_str, start_time, end_time) -> Shift:
        """创建班次。班次含护士、日期、起止时间。"""
        try:
            nurse, date_str, start_time, end_time = validate_shift_fields(
                nurse, date_str, start_time, end_time
            )
        except RuleValidationError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc

        with self._lock:
            shift = Shift(
                id=self.storage.issue_shift_id(),
                nurse=nurse,
                date=date_str,
                start_time=start_time,
                end_time=end_time,
                created_at=self._now_iso(),
            )
            self.storage.add_shift(shift)
            return shift

    def list_shifts(self, nurse: Optional[str] = None) -> list[Shift]:
        with self._lock:
            if nurse is not None:
                return self.storage.shifts_of_nurse(nurse)
            return self.storage.all_shifts()

    def get_shift_or_404(self, shift_id) -> Shift:
        try:
            shift_id = int(shift_id)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "invalid_request", "shift_id 必须是整数") from exc
        shift = self.storage.get_shift(shift_id)
        if shift is None:
            raise ApiError(404, "shift_not_found", f"班次 {shift_id} 不存在")
        return shift

    # ================= 换班：提交 =================

    def submit_swap(self, shift_id, original_nurse, target_nurse) -> Swap:
        """原护士提交换班。以下任一情况拒绝（409）：

        1. 目标护士同日已有时间重叠的班次；
        2. 原班次已经开始；
        3. 该班次已有未处理（待处理/待交接）的换班单。
        """
        try:
            shift_id = int(shift_id)
            original_nurse = validate_nurse(original_nurse, "original_nurse")
            target_nurse = validate_nurse(target_nurse, "target_nurse")
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "invalid_request", "shift_id 必须是整数") from exc
        except RuleValidationError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc

        if original_nurse == target_nurse:
            raise ApiError(
                400, "invalid_request", "接替人不能与原护士是同一个人"
            )

        with self._lock:
            shift = self.storage.get_shift(shift_id)
            if shift is None:
                raise ApiError(404, "shift_not_found", f"班次 {shift_id} 不存在")

            if shift.nurse != original_nurse:
                raise ApiError(
                    403,
                    "forbidden",
                    f"班次 {shift_id} 不属于护士 {original_nurse}",
                )

            # 拒绝条件 2：原班次已开始
            if is_shift_started(shift, self.now()):
                raise ApiError(
                    409,
                    "shift_already_started",
                    f"班次 {shift_id} 已开始，不能提交换班",
                )

            # 拒绝条件 3：已有未处理换班
            if has_unresolved_swap(self.storage.all_swaps(), shift_id):
                raise ApiError(
                    409,
                    "swap_in_progress",
                    f"班次 {shift_id} 已有未处理的换班",
                )

            # 拒绝条件 1：目标护士同日班次时间重叠
            if nurse_has_overlap(
                self.storage.all_shifts(),
                target_nurse,
                shift.date,
                shift.start_time,
                shift.end_time,
            ):
                raise ApiError(
                    409,
                    "target_schedule_conflict",
                    f"护士 {target_nurse} 在 {shift.date} 已有时间重叠的班次",
                )

            swap = Swap(
                id=self.storage.issue_swap_id(),
                shift_id=shift_id,
                original_nurse=original_nurse,
                target_nurse=target_nurse,
                status=PENDING,
                created_at=self._now_iso(),
            )
            self.storage.add_swap(swap)
            return swap

    # ================= 换班：接替 =================

    def accept_swap(self, swap_id, nurse) -> Swap:
        """接替人接受换班：待处理 -> 待交接（先等原护士确认）。

        接受瞬间再做一次防御性检查：原班次仍未开始、接替人同日仍无重叠班次。
        （提交时已按规则校验，这里防止提交后情况变化导致交接后冲突。）
        """
        swap = self._get_swap_or_404(swap_id)
        try:
            nurse = validate_nurse(nurse, "nurse")
        except RuleValidationError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc

        with self._lock:
            if nurse != swap.target_nurse:
                raise ApiError(403, "forbidden", "只有接替人可以接受该换班")
            if swap.status != PENDING:
                raise ApiError(
                    409,
                    "swap_not_pending",
                    f"换班 {swap.id} 当前不是待处理状态，无法接受",
                )

            shift = self.storage.get_shift(swap.shift_id)
            if shift is None:
                raise ApiError(404, "shift_not_found", "换班关联的班次不存在")
            if is_shift_started(shift, self.now()):
                raise ApiError(
                    409, "shift_already_started", "原班次已开始，不能接受换班"
                )
            if nurse_has_overlap(
                self.storage.all_shifts(),
                nurse,
                shift.date,
                shift.start_time,
                shift.end_time,
                exclude_shift_id=shift.id,
            ):
                raise ApiError(
                    409,
                    "target_schedule_conflict",
                    f"护士 {nurse} 在 {shift.date} 已有时间重叠的班次",
                )

            swap.status = HANDOVER
            swap.confirm_stage = STAGE_AWAIT_ORIGINAL
            swap.accepted_at = self._now_iso()
            self.storage.update_swap(swap)
            return swap

    # ================= 换班：确认 =================

    def confirm_swap(self, swap_id, nurse) -> Swap:
        """双方依次确认，两人都确认才完成：

        - 第一次确认：原护士（handover/awaiting_original -> awaiting_target）；
        - 第二次确认：接替人（awaiting_target -> completed，班次转交）。
        """
        swap = self._get_swap_or_404(swap_id)
        try:
            nurse = validate_nurse(nurse, "nurse")
        except RuleValidationError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc

        with self._lock:
            if swap.status != HANDOVER:
                raise ApiError(
                    409,
                    "swap_not_in_handover",
                    f"换班 {swap.id} 当前不是待交接状态，无法确认",
                )

            # 第一步：原护士确认
            if swap.confirm_stage == STAGE_AWAIT_ORIGINAL:
                if nurse != swap.original_nurse:
                    raise ApiError(
                        403,
                        "forbidden",
                        "当前需要原护士先确认，且只有原护士本人可以确认",
                    )
                swap.original_confirmed = True
                swap.confirm_stage = STAGE_AWAIT_TARGET
                self.storage.update_swap(swap)
                return swap

            # 第二步：接替人确认 -> 完成，班次转交
            if nurse != swap.target_nurse:
                raise ApiError(
                    403,
                    "forbidden",
                    "当前需要接替人确认，且只有接替人本人可以确认",
                )

            shift = self.storage.get_shift(swap.shift_id)
            if shift is None:
                raise ApiError(404, "shift_not_found", "换班关联的班次不存在")

            # 完成前的最终防御性检查：班次仍未开始且无同日重叠
            if is_shift_started(shift, self.now()):
                raise ApiError(
                    409,
                    "shift_already_started",
                    "原班次已开始，不能完成换班",
                )
            if nurse_has_overlap(
                self.storage.all_shifts(),
                nurse,
                shift.date,
                shift.start_time,
                shift.end_time,
                exclude_shift_id=shift.id,
            ):
                raise ApiError(
                    409,
                    "target_schedule_conflict",
                    f"护士 {nurse} 在 {shift.date} 已有时间重叠的班次",
                )

            swap.target_confirmed = True
            swap.confirm_stage = None
            swap.status = COMPLETED
            swap.completed_at = self._now_iso()
            # 班次实际转交给接替人
            shift.nurse = swap.target_nurse
            self.storage.update_shift(shift)
            self.storage.update_swap(swap)
            return swap

    # ================= 换班：撤回 =================

    def withdraw_swap(self, swap_id, nurse) -> Swap:
        """第二次确认（接替人确认完成）之前，原护士可以撤回。

        撤回后回到“等待原护士确认”阶段，原护士此前的确认作废，
        双方需要按原护士 -> 接替人的顺序重新确认。
        """
        swap = self._get_swap_or_404(swap_id)
        try:
            nurse = validate_nurse(nurse, "nurse")
        except RuleValidationError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc

        with self._lock:
            if nurse != swap.original_nurse:
                raise ApiError(403, "forbidden", "只有原护士可以撤回换班")
            if swap.status != HANDOVER:
                raise ApiError(
                    409,
                    "swap_not_in_handover",
                    f"换班 {swap.id} 不是待交接状态，无法撤回",
                )

            swap.original_confirmed = False
            swap.target_confirmed = False
            swap.confirm_stage = STAGE_AWAIT_ORIGINAL
            self.storage.update_swap(swap)
            return swap

    # ================= 查询 =================

    def get_swap(self, swap_id) -> Swap:
        return self._get_swap_or_404(swap_id)

    def list_swaps(self, nurse: Optional[str] = None, status: Optional[str] = None) -> list[Swap]:
        with self._lock:
            swaps = (
                self.storage.swaps_of_nurse(nurse)
                if nurse is not None
                else self.storage.all_swaps()
            )
            if status is not None:
                if status not in (PENDING, HANDOVER, COMPLETED):
                    raise ApiError(
                        400,
                        "invalid_request",
                        "status 只能是 pending / handover / completed",
                    )
                swaps = [s for s in swaps if s.status == status]
            return swaps

    # ================= 内部 =================

    def _get_swap_or_404(self, swap_id) -> Swap:
        try:
            swap_id = int(swap_id)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "invalid_request", "swap_id 必须是整数") from exc
        swap = self.storage.get_swap(swap_id)
        if swap is None:
            raise ApiError(404, "swap_not_found", f"换班单 {swap_id} 不存在")
        return swap
