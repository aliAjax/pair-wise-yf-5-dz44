"""service 层：完整换班生命周期与拒绝规则测试。"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from nurse_swap.errors import ApiError
from nurse_swap.models import (
    COMPLETED,
    HANDOVER,
    PENDING,
    STAGE_AWAIT_ORIGINAL,
    STAGE_AWAIT_TARGET,
)
from nurse_swap.service import NurseSwapService
from nurse_swap.storage import JsonFileStorage

DAY = "2026-10-15"


class MutableClock:
    def __init__(self, current: datetime):
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs):
        self.current += timedelta(**kwargs)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = MutableClock(datetime(2026, 10, 14, 9, 0))
        self.storage = JsonFileStorage(str(Path(self.tmp.name) / "db.json"))
        self.service = NurseSwapService(self.storage, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def add_shift(self, nurse, start="08:00", end="12:00", date=DAY):
        return self.service.create_shift(
            nurse=nurse, date_str=date, start_time=start, end_time=end
        )

    def full_swap(self, shift_id, original="Alice", target="Bob"):
        swap = self.service.submit_swap(shift_id, original, target)
        swap = self.service.accept_swap(swap.id, target)
        swap = self.service.confirm_swap(swap.id, original)
        swap = self.service.confirm_swap(swap.id, target)
        return swap

    def assert_api_error(self, status, code, fn, *args, **kwargs):
        with self.assertRaises(ApiError) as cm:
            fn(*args, **kwargs)
        self.assertEqual(cm.exception.status, status)
        self.assertEqual(cm.exception.code, code)
        return cm.exception

    # ---------- 创建 / 查询 ----------

    def test_create_and_query_shift(self):
        shift = self.add_shift("Alice")
        self.assertEqual(shift.id, 1)
        self.assertEqual(self.service.get_shift_or_404(1).nurse, "Alice")
        self.assertEqual([s.id for s in self.service.list_shifts("Alice")], [1])
        self.assertEqual(self.service.list_shifts("Bob"), [])

    def test_create_shift_invalid(self):
        self.assert_api_error(
            400, "invalid_request", self.add_shift, "Alice", "12:00", "08:00"
        )

    # ---------- 提交拒绝条件 ----------

    def test_submit_rejects_not_owner(self):
        shift = self.add_shift("Alice")
        self.assert_api_error(
            403, "forbidden", self.service.submit_swap, shift.id, "Carol", "Bob"
        )

    def test_submit_rejects_target_overlap(self):
        self.add_shift("Alice", "08:00", "12:00")
        self.add_shift("Bob", "11:00", "13:00")
        self.assert_api_error(
            409, "target_schedule_conflict",
            self.service.submit_swap, 1, "Alice", "Bob",
        )

    def test_submit_allows_adjacent(self):
        self.add_shift("Alice", "08:00", "12:00")
        self.add_shift("Bob", "12:00", "13:00")
        self.assertEqual(
            self.service.submit_swap(1, "Alice", "Bob").status, PENDING
        )

    def test_submit_allows_other_day_overlap(self):
        self.add_shift("Alice", "08:00", "12:00")
        self.add_shift("Bob", "08:00", "12:00", date="2026-10-16")
        self.assertEqual(
            self.service.submit_swap(1, "Alice", "Bob").status, PENDING
        )

    def test_submit_rejects_started(self):
        self.add_shift("Alice", "08:00", "12:00")
        self.clock.current = datetime(2026, 10, 15, 8, 0)
        self.assert_api_error(
            409, "shift_already_started",
            self.service.submit_swap, 1, "Alice", "Bob",
        )

    def test_submit_rejects_unresolved(self):
        self.add_shift("Alice")
        self.service.submit_swap(1, "Alice", "Bob")
        self.assert_api_error(
            409, "swap_in_progress",
            self.service.submit_swap, 1, "Alice", "Carol",
        )

    def test_submit_again_after_completed(self):
        shift = self.add_shift("Alice")
        self.full_swap(shift.id)
        self.add_shift("Carol", "13:00", "14:00")
        self.assertEqual(
            self.service.submit_swap(1, "Bob", "Carol").status, PENDING
        )

    def test_submit_same_nurse(self):
        self.add_shift("Alice")
        self.assertRaises(ApiError, self.service.submit_swap, 1, "Alice", "Alice")

    # ---------- 接替 / 确认顺序 ----------

    def test_only_target_accepts(self):
        self.add_shift("Alice")
        self.service.submit_swap(1, "Alice", "Bob")
        self.assert_api_error(403, "forbidden", self.service.accept_swap, 1, "Alice")

    def test_accept_moves_to_handover(self):
        self.add_shift("Alice")
        self.service.submit_swap(1, "Alice", "Bob")
        swap = self.service.accept_swap(1, "Bob")
        self.assertEqual(swap.status, HANDOVER)
        self.assertEqual(swap.confirm_stage, STAGE_AWAIT_ORIGINAL)
        self.assertIsNotNone(swap.accepted_at)

    def test_confirm_order(self):
        self.add_shift("Alice")
        self.service.submit_swap(1, "Alice", "Bob")
        self.service.accept_swap(1, "Bob")
        # 接替人抢先确认 -> 403
        self.assert_api_error(
            403, "forbidden", self.service.confirm_swap, 1, "Bob"
        )
        # 陌生人不能确认
        self.assert_api_error(
            403, "forbidden", self.service.confirm_swap, 1, "X"
        )

    def test_second_confirm_completes_and_transfers(self):
        shift = self.add_shift("Alice")
        self.full_swap(shift.id)
        swap = self.service.get_swap(1)
        self.assertEqual(swap.status, COMPLETED)
        self.assertTrue(swap.original_confirmed)
        self.assertTrue(swap.target_confirmed)
        self.assertIsNone(swap.confirm_stage)
        self.assertIsNotNone(swap.completed_at)
        self.assertEqual(self.storage.get_shift(shift.id).nurse, "Bob")

    def test_one_confirm_not_completed(self):
        self.add_shift("Alice")
        self.service.submit_swap(1, "Alice", "Bob")
        self.service.accept_swap(1, "Bob")
        swap = self.service.confirm_swap(1, "Alice")
        self.assertEqual(swap.status, HANDOVER)
        self.assertEqual(swap.confirm_stage, STAGE_AWAIT_TARGET)
        self.assertEqual(self.service.get_shift_or_404(1).nurse, "Alice")

    def test_cannot_confirm_pending(self):
        self.add_shift("Alice")
        self.service.submit_swap(1, "Alice", "Bob")
        self.assert_api_error(
            409, "swap_not_in_handover", self.service.confirm_swap, 1, "Alice"
        )

    def test_accept_twice(self):
        self.add_shift("Alice")
        self.service.submit_swap(1, "Alice", "Bob")
        self.service.accept_swap(1, "Bob")
        self.assert_api_error(
            409, "swap_not_pending", self.service.accept_swap, 1, "Bob"
        )

    # ---------- 撤回 ----------

    def test_withdraw_after_first_confirm(self):
        self.add_shift("Alice")
        self.service.submit_swap(1, "Alice", "Bob")
        self.service.accept_swap(1, "Bob")
        self.service.confirm_swap(1, "Alice")
        self.assertEqual(self.service.get_swap(1).confirm_stage, STAGE_AWAIT_TARGET)

        swap = self.service.withdraw_swap(1, "Alice")
        self.assertEqual(swap.status, HANDOVER)
        self.assertEqual(swap.confirm_stage, STAGE_AWAIT_ORIGINAL)
        self.assertFalse(swap.original_confirmed)
        self.assertFalse(swap.target_confirmed)

        # 此时接替人不能确认
        self.assert_api_error(
            403, "forbidden", self.service.confirm_swap, 1, "Bob"
        )
        # 重新走一遍可以完成
        self.service.confirm_swap(1, "Alice")
        swap = self.service.confirm_swap(1, "Bob")
        self.assertEqual(swap.status, COMPLETED)

    def test_withdraw_before_any_confirm(self):
        self.add_shift("Alice")
        self.service.submit_swap(1, "Alice", "Bob")
        self.service.accept_swap(1, "Bob")
        swap = self.service.withdraw_swap(1, "Alice")
        self.assertEqual(swap.confirm_stage, STAGE_AWAIT_ORIGINAL)

    def test_only_original_withdraws(self):
        self.add_shift("Alice")
        self.service.submit_swap(1, "Alice", "Bob")
        self.service.accept_swap(1, "Bob")
        self.assert_api_error(
            403, "forbidden", self.service.withdraw_swap, 1, "Bob"
        )

    def test_withdraw_wrong_status(self):
        self.add_shift("Alice")
        self.service.submit_swap(1, "Alice", "Bob")
        self.assert_api_error(
            409, "swap_not_in_handover", self.service.withdraw_swap, 1, "Alice"
        )
        # 推进到已完成，再撤回同样被拒
        self.service.accept_swap(1, "Bob")
        self.service.confirm_swap(1, "Alice")
        self.service.confirm_swap(1, "Bob")
        self.assert_api_error(
            409, "swap_not_in_handover", self.service.withdraw_swap, 1, "Alice"
        )

    # ---------- 接受时的最终冲突检查 ----------

    def test_accept_rejects_late_overlap(self):
        self.add_shift("Alice", "08:00", "12:00")
        self.add_shift("Bob", "13:00", "14:00")
        self.service.submit_swap(1, "Alice", "Bob")
        self.add_shift("Bob", "10:00", "11:00")
        self.assert_api_error(
            409, "target_schedule_conflict", self.service.accept_swap, 1, "Bob"
        )

    # ---------- 查询 ----------

    def test_list_swaps(self):
        self.add_shift("Alice", "08:00", "12:00")
        self.add_shift("Bob", "13:00", "18:00")
        self.service.submit_swap(1, "Alice", "Bob")
        self.service.submit_swap(2, "Bob", "Alice")
        self.service.accept_swap(2, "Alice")

        self.assertEqual(
            {s.id for s in self.service.list_swaps(nurse="Alice")}, {1, 2}
        )
        self.assertEqual(
            {s.id for s in self.service.list_swaps(nurse="Bob")}, {1, 2}
        )
        self.assertEqual(set(self.service.list_swaps(nurse="Z")), set())
        self.assertEqual(
            {s.id for s in self.service.list_swaps(status=PENDING)}, {1}
        )
        self.assertEqual(
            [s.id for s in self.service.list_swaps(nurse="Alice", status=HANDOVER)],
            [2],
        )

    def test_404s(self):
        self.assert_api_error(404, "shift_not_found", self.service.get_shift_or_404, 999)
        self.assert_api_error(404, "swap_not_found", self.service.accept_swap, 999, "B")


if __name__ == "__main__":
    unittest.main()
