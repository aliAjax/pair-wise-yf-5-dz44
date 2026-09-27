"""rules 层纯函数测试。"""

import unittest
from datetime import datetime

from nurse_swap.models import HANDOVER, PENDING, COMPLETED, Shift, Swap
from nurse_swap.rules import (
    RuleValidationError,
    has_unresolved_swap,
    is_shift_started,
    nurse_has_overlap,
    shifts_overlap,
    validate_shift_fields,
)


def make_shift(sid=1, nurse="Alice", date="2026-10-01", start="08:00", end="12:00"):
    return Shift(
        id=sid, nurse=nurse, date=date, start_time=start, end_time=end,
        created_at="2026-09-01T00:00:00",
    )


def make_swap(swid, shift_id, status):
    return Swap(
        id=swid, shift_id=shift_id, original_nurse="A", target_nurse="B",
        status=status,
    )


class RulesTest(unittest.TestCase):
    def test_validate_ok(self):
        self.assertEqual(
            validate_shift_fields(" A ", "2026-10-01", "08:00", "12:00"),
            ("A", "2026-10-01", "08:00", "12:00"),
        )

    def test_validate_bad(self):
        bad = [
            ("", "2026-10-01", "08:00", "12:00"),
            ("A", "2026-10-1", "08:00", "12:00"),
            ("A", "2026-10-01", "8:00", "12:00"),
            ("A", "2026-10-01", "24:00", "12:00"),
            ("A", "2026-10-01", "12:00", "12:00"),
            ("A", "2026-10-01", "13:00", "12:00"),
            ("A", "2026-02-30", "08:00", "12:00"),
        ]
        for nurse, date, start, end in bad:
            with self.subTest(args=(nurse, date, start, end)):
                with self.assertRaises(RuleValidationError):
                    validate_shift_fields(nurse, date, start, end)

    def test_overlap(self):
        s = make_shift()
        self.assertTrue(shifts_overlap(s, "2026-10-01", "11:30", "13:00"))
        self.assertTrue(shifts_overlap(s, "2026-10-01", "09:00", "10:00"))
        # 首尾相接不算重叠
        self.assertFalse(shifts_overlap(s, "2026-10-01", "12:00", "13:00"))
        self.assertFalse(shifts_overlap(s, "2026-10-01", "06:00", "08:00"))
        self.assertFalse(shifts_overlap(s, "2026-10-02", "09:00", "10:00"))

    def test_nurse_overlap_filter(self):
        shifts = [make_shift(nurse="A"), make_shift(sid=2, nurse="B", start="13:00", end="14:00")]
        self.assertTrue(nurse_has_overlap(shifts, "A", "2026-10-01", "09:00", "10:00"))
        self.assertFalse(nurse_has_overlap(shifts, "B", "2026-10-01", "09:00", "10:00"))
        self.assertFalse(
            nurse_has_overlap(shifts, "A", "2026-10-01", "09:00", "10:00",
                              exclude_shift_id=1)
        )

    def test_started(self):
        s = make_shift(start="08:00")
        self.assertTrue(is_shift_started(s, datetime(2026, 10, 1, 8, 0)))
        self.assertTrue(is_shift_started(s, datetime(2026, 10, 1, 9, 0)))
        self.assertFalse(is_shift_started(s, datetime(2026, 10, 1, 7, 59)))

    def test_unresolved(self):
        swaps = [
            make_swap(1, 10, PENDING),
            make_swap(2, 20, HANDOVER),
            make_swap(3, 30, COMPLETED),
        ]
        self.assertTrue(has_unresolved_swap(swaps, 10))
        self.assertTrue(has_unresolved_swap(swaps, 20))
        self.assertFalse(has_unresolved_swap(swaps, 30))
        self.assertFalse(has_unresolved_swap(swaps, 99))


if __name__ == "__main__":
    unittest.main()
