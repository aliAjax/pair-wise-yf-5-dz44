"""服务层与规则测试: 换班状态机、三类拒绝条件、依次确认、撤回、重启持久化。"""
import os
import tempfile
import unittest
from datetime import date, datetime, time

from nurse_swap import (
    NotFoundError,
    RuleViolation,
    Store,
    SwapService,
    SwapState,
)

NOW = datetime(2026, 9, 26, 8, 0, 0)       # 固定的"当前时间"
DAY = date(2026, 9, 27)                     # 默认班次日期(未来)


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.store = Store(self.db_path)
        self.now = NOW
        self.service = SwapService(self.store, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def mkshift(self, nurse="nurse-a", day=DAY, start=time(8), end=time(16)):
        return self.service.create_shift(nurse, day, start, end)

    def open_swap(self, nurse="nurse-a", acceptor="nurse-b", **shift_kw):
        """构造一个已被接替的换班申请。"""
        shift = self.mkshift(nurse=nurse, **shift_kw)
        req = self.service.submit_swap(shift.id, nurse)
        self.service.accept_swap(req.id, acceptor)
        return shift, req

    # ---------------- 创建班次 ----------------

    def test_create_shift_ok(self):
        shift = self.mkshift()
        self.assertEqual(shift.nurse_id, "nurse-a")
        self.assertEqual(shift.to_dict()["date"], "2026-09-27")
        self.assertEqual(shift.to_dict()["start_time"], "08:00")

    def test_create_shift_rejects_bad_time_range(self):
        with self.assertRaises(RuleViolation):
            self.mkshift(start=time(16), end=time(8))
        with self.assertRaises(RuleViolation):
            self.mkshift(start=time(8), end=time(8))

    # ---------------- 提交换班 ----------------

    def test_submit_swap_ok(self):
        shift = self.mkshift()
        req = self.service.submit_swap(shift.id, "nurse-a")
        self.assertEqual(req.state, SwapState.PENDING)
        self.assertEqual(req.requester_id, "nurse-a")

    def test_submit_rejects_missing_shift(self):
        with self.assertRaises(NotFoundError):
            self.service.submit_swap("no-such-shift", "nurse-a")

    def test_submit_rejects_non_owner(self):
        shift = self.mkshift()
        with self.assertRaises(RuleViolation):
            self.service.submit_swap(shift.id, "nurse-b")

    def test_submit_rejects_started_shift(self):
        # 今天 07:00 开始的班次, 当前 08:00, 已开始
        shift = self.mkshift(day=date(2026, 9, 26), start=time(7), end=time(15))
        with self.assertRaises(RuleViolation):
            self.service.submit_swap(shift.id, "nurse-a")

    def test_submit_rejects_shift_starting_exactly_now(self):
        shift = self.mkshift(day=date(2026, 9, 26), start=time(8), end=time(16))
        with self.assertRaises(RuleViolation):
            self.service.submit_swap(shift.id, "nurse-a")

    def test_submit_rejects_duplicate_open_request(self):
        shift = self.mkshift()
        self.service.submit_swap(shift.id, "nurse-a")
        with self.assertRaises(RuleViolation):  # 待处理中重复提交
            self.service.submit_swap(shift.id, "nurse-a")

    def test_submit_rejects_when_previous_request_accepted(self):
        shift, _ = self.open_swap()
        with self.assertRaises(RuleViolation):  # 待交接中也算未处理
            self.service.submit_swap(shift.id, "nurse-a")

    # ---------------- 接替 ----------------

    def test_accept_ok(self):
        shift = self.mkshift()
        req = self.service.submit_swap(shift.id, "nurse-a")
        req = self.service.accept_swap(req.id, "nurse-b")
        self.assertEqual(req.state, SwapState.ACCEPTED)
        self.assertEqual(req.acceptor_id, "nurse-b")

    def test_accept_rejects_overlapping_shift_same_day(self):
        self.mkshift(nurse="nurse-b", start=time(12), end=time(20))  # 与 08-16 重叠
        shift = self.mkshift()
        req = self.service.submit_swap(shift.id, "nurse-a")
        with self.assertRaises(RuleViolation):
            self.service.accept_swap(req.id, "nurse-b")

    def test_accept_allows_adjacent_shift(self):
        self.mkshift(nurse="nurse-b", start=time(16), end=time(23))  # 首尾相接不算重叠
        shift = self.mkshift()
        req = self.service.submit_swap(shift.id, "nurse-a")
        req = self.service.accept_swap(req.id, "nurse-b")
        self.assertEqual(req.state, SwapState.ACCEPTED)

    def test_accept_allows_same_hours_other_day(self):
        self.mkshift(nurse="nurse-b", day=date(2026, 9, 28))  # 不同日同时段不冲突
        shift = self.mkshift()
        req = self.service.submit_swap(shift.id, "nurse-a")
        self.service.accept_swap(req.id, "nurse-b")

    def test_accept_rejects_self(self):
        shift = self.mkshift()
        req = self.service.submit_swap(shift.id, "nurse-a")
        with self.assertRaises(RuleViolation):
            self.service.accept_swap(req.id, "nurse-a")

    def test_accept_rejects_when_not_pending(self):
        _, req = self.open_swap()
        with self.assertRaises(RuleViolation):
            self.service.accept_swap(req.id, "nurse-c")

    def test_accept_rejects_after_shift_started(self):
        shift = self.mkshift(day=date(2026, 9, 26), start=time(9), end=time(17))
        req = self.service.submit_swap(shift.id, "nurse-a")  # 08:00 提交时未开始
        self.now = datetime(2026, 9, 26, 9, 30)              # 时间推进到班次开始后
        with self.assertRaises(RuleViolation):
            self.service.accept_swap(req.id, "nurse-b")

    # ---------------- 确认(依次) ----------------

    def test_confirm_must_follow_order(self):
        _, req = self.open_swap()
        with self.assertRaises(RuleViolation):  # 接替人不能先确认
            self.service.confirm_swap(req.id, "nurse-b")
        req = self.service.confirm_swap(req.id, "nurse-a")
        self.assertTrue(req.requester_confirmed)
        self.assertEqual(req.state, SwapState.ACCEPTED)  # 第一次确认后仍未完成
        with self.assertRaises(RuleViolation):  # 原护士不能重复确认
            self.service.confirm_swap(req.id, "nurse-a")
        with self.assertRaises(RuleViolation):  # 无关人员不能确认
            self.service.confirm_swap(req.id, "nurse-c")
        req = self.service.confirm_swap(req.id, "nurse-b")
        self.assertEqual(req.state, SwapState.COMPLETED)

    def test_confirm_rejects_when_not_accepted(self):
        shift = self.mkshift()
        req = self.service.submit_swap(shift.id, "nurse-a")
        with self.assertRaises(RuleViolation):
            self.service.confirm_swap(req.id, "nurse-a")

    def test_completion_reassigns_shift(self):
        shift, req = self.open_swap()
        self.service.confirm_swap(req.id, "nurse-a")
        self.service.confirm_swap(req.id, "nurse-b")
        self.assertEqual(self.store.get_shift(shift.id).nurse_id, "nurse-b")

    def test_completed_shift_can_be_swapped_again_by_new_owner(self):
        shift, req = self.open_swap()
        self.service.confirm_swap(req.id, "nurse-a")
        self.service.confirm_swap(req.id, "nurse-b")
        req2 = self.service.submit_swap(shift.id, "nurse-b")  # 已完成不阻塞新申请
        self.assertEqual(req2.state, SwapState.PENDING)

    # ---------------- 撤回 ----------------

    def test_withdraw_resets_to_pending_and_voids_confirmation(self):
        _, req = self.open_swap()
        self.service.confirm_swap(req.id, "nurse-a")          # 第一次确认
        req = self.service.withdraw_swap(req.id, "nurse-a")   # 第二次确认前撤回
        self.assertEqual(req.state, SwapState.PENDING)
        self.assertIsNone(req.acceptor_id)
        self.assertFalse(req.requester_confirmed)             # 前一人的确认作废
        self.assertFalse(req.acceptor_confirmed)

    def test_withdraw_allowed_before_first_confirmation(self):
        _, req = self.open_swap()
        req = self.service.withdraw_swap(req.id, "nurse-a")
        self.assertEqual(req.state, SwapState.PENDING)

    def test_withdraw_rejects_non_requester(self):
        _, req = self.open_swap()
        with self.assertRaises(RuleViolation):
            self.service.withdraw_swap(req.id, "nurse-b")

    def test_withdraw_rejects_when_pending(self):
        shift = self.mkshift()
        req = self.service.submit_swap(shift.id, "nurse-a")
        with self.assertRaises(RuleViolation):
            self.service.withdraw_swap(req.id, "nurse-a")

    def test_withdraw_rejects_after_completed(self):
        _, req = self.open_swap()
        self.service.confirm_swap(req.id, "nurse-a")
        self.service.confirm_swap(req.id, "nurse-b")
        with self.assertRaises(RuleViolation):
            self.service.withdraw_swap(req.id, "nurse-a")

    def test_reaccept_after_withdraw_starts_fresh(self):
        shift, req = self.open_swap()
        self.service.confirm_swap(req.id, "nurse-a")
        self.service.withdraw_swap(req.id, "nurse-a")
        req = self.service.accept_swap(req.id, "nurse-c")     # 换人重新接替
        self.assertEqual(req.acceptor_id, "nurse-c")
        self.assertFalse(req.requester_confirmed)             # 确认不残留
        self.service.confirm_swap(req.id, "nurse-a")
        req = self.service.confirm_swap(req.id, "nurse-c")
        self.assertEqual(req.state, SwapState.COMPLETED)
        self.assertEqual(self.store.get_shift(shift.id).nurse_id, "nurse-c")

    # ---------------- 查询 ----------------

    def test_list_nurse_swaps_covers_requester_and_acceptor(self):
        _, req = self.open_swap()
        self.assertEqual([r.id for r, _ in self.service.list_nurse_swaps("nurse-a")], [req.id])
        self.assertEqual([r.id for r, _ in self.service.list_nurse_swaps("nurse-b")], [req.id])
        self.assertEqual(self.service.list_nurse_swaps("nurse-x"), [])

    def test_list_nurse_swaps_filter_by_state(self):
        _, req = self.open_swap()
        self.assertEqual(len(self.service.list_nurse_swaps("nurse-a", SwapState.ACCEPTED)), 1)
        self.assertEqual(len(self.service.list_nurse_swaps("nurse-a", SwapState.PENDING)), 0)

    # ---------------- 重启持久化 ----------------

    def test_restart_keeps_pending_accepted_completed(self):
        # 待处理
        s1 = self.mkshift()
        r1 = self.service.submit_swap(s1.id, "nurse-a")
        # 待交接
        s2 = self.mkshift(start=time(16), end=time(23))
        r2 = self.service.submit_swap(s2.id, "nurse-a")
        self.service.accept_swap(r2.id, "nurse-b")
        # 已完成
        s3 = self.mkshift(day=date(2026, 9, 28))
        r3 = self.service.submit_swap(s3.id, "nurse-a")
        self.service.accept_swap(r3.id, "nurse-b")
        self.service.confirm_swap(r3.id, "nurse-a")
        self.service.confirm_swap(r3.id, "nurse-b")

        # 模拟重启: 关闭后用同一数据库文件重建 Store 与 Service
        self.store.close()
        self.store = Store(self.db_path)
        self.service = SwapService(self.store, clock=lambda: self.now)

        states = {
            r.id: r.state for r, _ in self.service.list_nurse_swaps("nurse-a")
        }
        self.assertEqual(states, {
            r1.id: SwapState.PENDING,
            r2.id: SwapState.ACCEPTED,
            r3.id: SwapState.COMPLETED,
        })
        self.assertEqual(len(self.service.list_nurse_swaps("nurse-a", SwapState.PENDING)), 1)
        self.assertEqual(len(self.service.list_nurse_swaps("nurse-a", SwapState.ACCEPTED)), 1)
        self.assertEqual(len(self.service.list_nurse_swaps("nurse-a", SwapState.COMPLETED)), 1)
        # 已完成的班次归属变更也持久化
        self.assertEqual(self.store.get_shift(s3.id).nurse_id, "nurse-b")
        # 重启后流程可继续推进
        self.service.confirm_swap(r2.id, "nurse-a")
        req = self.service.confirm_swap(r2.id, "nurse-b")
        self.assertEqual(req.state, SwapState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
