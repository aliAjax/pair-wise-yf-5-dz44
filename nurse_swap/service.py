"""服务层: 编排规则校验(rules)与存储读写(storage), 不感知 HTTP。"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Callable

from . import rules
from .errors import RuleViolation
from .models import Shift, SwapRequest, SwapState
from .storage import Store


class SwapService:
    """护士换班服务的核心业务入口。

    clock 可注入以便测试; 默认使用本地当前时间判断"班次是否已开始"。
    """

    def __init__(self, store: Store, clock: Callable[[], datetime] = datetime.now):
        self._store = store
        self._clock = clock

    # ---------------- 班次 ----------------

    def create_shift(
        self, nurse_id: str, day: date, start_time: time, end_time: time
    ) -> Shift:
        if not nurse_id:
            raise RuleViolation("护士不能为空")
        if end_time <= start_time:
            raise RuleViolation("结束时间必须晚于开始时间(暂不支持跨天班次)")
        shift = Shift(nurse_id=nurse_id, day=day, start_time=start_time, end_time=end_time)
        self._store.add_shift(shift)
        return shift

    def list_nurse_shifts(self, nurse_id: str) -> list[Shift]:
        return self._store.list_shifts_by_nurse(nurse_id)

    # ---------------- 换班流程 ----------------

    def submit_swap(self, shift_id: str, requester_id: str) -> SwapRequest:
        """提交换班: 原护士为自己的未开始班次发起申请, 进入待处理。"""
        shift = self._store.get_shift(shift_id)
        rules.ensure_requester_owns_shift(shift, requester_id)
        rules.ensure_shift_not_started(shift, self._clock())
        rules.ensure_no_open_request(self._store.find_open_request_for_shift(shift_id))
        now = self._clock()
        request = SwapRequest(
            shift_id=shift.id, requester_id=requester_id,
            created_at=now, updated_at=now,
        )
        self._store.add_request(request)
        return request

    def accept_swap(self, request_id: str, acceptor_id: str) -> SwapRequest:
        """接替: 其他护士接过班次, 进入待交接。"""
        request = self._store.get_request(request_id)
        shift = self._store.get_shift(request.shift_id)
        acceptor_shifts = self._store.list_shifts_by_nurse_on_day(acceptor_id, shift.day)
        rules.ensure_acceptable(request, shift, acceptor_id, acceptor_shifts, self._clock())
        request.acceptor_id = acceptor_id
        request.state = SwapState.ACCEPTED
        request.updated_at = self._clock()
        self._store.update_request(request)
        return request

    def confirm_swap(self, request_id: str, nurse_id: str) -> SwapRequest:
        """确认: 原护士先确认, 接替人再确认; 双方都确认后完成并变更班次归属。"""
        request = self._store.get_request(request_id)
        role = rules.ensure_confirm_turn(request, nurse_id)
        if role == "requester":
            request.requester_confirmed = True
        else:
            request.acceptor_confirmed = True
            request.state = SwapState.COMPLETED
            shift = self._store.get_shift(request.shift_id)
            shift.nurse_id = request.acceptor_id
            self._store.update_shift(shift)
        request.updated_at = self._clock()
        self._store.update_request(request)
        return request

    def withdraw_swap(self, request_id: str, nurse_id: str) -> SwapRequest:
        """撤回: 第二次确认前原护士可撤回, 回到待处理, 接替人与此前确认一并作废。"""
        request = self._store.get_request(request_id)
        rules.ensure_withdrawable(request, nurse_id)
        request.state = SwapState.PENDING
        request.acceptor_id = None
        request.requester_confirmed = False
        request.acceptor_confirmed = False
        request.updated_at = self._clock()
        self._store.update_request(request)
        return request

    # ---------------- 查询 ----------------

    def get_swap(self, request_id: str) -> tuple[SwapRequest, Shift]:
        request = self._store.get_request(request_id)
        return request, self._store.get_shift(request.shift_id)

    def list_nurse_swaps(
        self, nurse_id: str, state: SwapState | None = None
    ) -> list[tuple[SwapRequest, Shift]]:
        """按护士查询换班申请(作为原护士或接替人), 可按状态过滤。"""
        requests = self._store.list_requests_by_nurse(nurse_id, state)
        return [(r, self._store.get_shift(r.shift_id)) for r in requests]
