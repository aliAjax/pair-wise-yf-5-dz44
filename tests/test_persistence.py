"""重启恢复：同一数据文件重新加载，三种状态的换班单都仍可查。"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from nurse_swap.errors import ApiError
from nurse_swap.models import COMPLETED, HANDOVER, PENDING, STAGE_AWAIT_TARGET
from nurse_swap.service import NurseSwapService
from nurse_swap.storage import JsonFileStorage

DAY = "2026-10-20"


def make_service(path):
    clock = lambda: datetime(2020, 1, 1)  # noqa: E731 - 固定时钟，保证班次未开始
    return NurseSwapService(JsonFileStorage(path), clock=clock)


class PersistenceTest(unittest.TestCase):
    def test_statuses_survive_restart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "db.json")

        service = make_service(db)
        # 1) 待处理
        service.create_shift("Alice", DAY, "08:00", "12:00")
        service.submit_swap(1, "Alice", "Bob")
        # 2) 待交接（原护士已确认，等接替人第二次确认）
        service.create_shift("Carol", DAY, "13:00", "18:00")
        service.submit_swap(2, "Carol", "Dave")
        service.accept_swap(2, "Dave")
        service.confirm_swap(2, "Carol")
        # 3) 已完成
        service.create_shift("Erin", DAY, "18:00", "22:00")
        service.submit_swap(3, "Erin", "Frank")
        service.accept_swap(3, "Frank")
        service.confirm_swap(3, "Erin")
        service.confirm_swap(3, "Frank")

        # —— 模拟重启 ——
        restarted = make_service(db)

        swaps = {s.id: s for s in restarted.list_swaps()}
        self.assertEqual(swaps[1].status, PENDING)
        self.assertEqual(swaps[2].status, HANDOVER)
        self.assertEqual(swaps[2].confirm_stage, STAGE_AWAIT_TARGET)
        self.assertTrue(swaps[2].original_confirmed)
        self.assertEqual(swaps[3].status, COMPLETED)

        shifts = {s.id: s for s in restarted.list_shifts()}
        self.assertEqual(shifts[1].nurse, "Alice")
        self.assertEqual(shifts[2].nurse, "Carol")
        self.assertEqual(shifts[3].nurse, "Frank")  # 完成的转交持久化

        # ID 计数器恢复
        self.assertEqual(restarted.create_shift("Gina", DAY, "06:00", "07:00").id, 4)
        self.assertEqual(restarted.submit_swap(4, "Gina", "Bob").id, 4)

        # 待交接单子可从断点继续
        swap = restarted.confirm_swap(2, "Dave")
        self.assertEqual(swap.status, COMPLETED)
        self.assertEqual(restarted.get_shift_or_404(2).nurse, "Dave")

        # 待处理单子重启后仍占名额
        with self.assertRaises(ApiError) as cm:
            restarted.submit_swap(1, "Alice", "Carol")
        self.assertEqual(cm.exception.code, "swap_in_progress")

        # 按护士 / 按状态查询
        self.assertEqual({s.id for s in restarted.list_swaps(nurse="Carol")}, {2})
        self.assertEqual(
            {s.id for s in restarted.list_swaps(status=COMPLETED)}, {2, 3}
        )


if __name__ == "__main__":
    unittest.main()
