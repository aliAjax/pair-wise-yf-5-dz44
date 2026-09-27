"""持久化层：JSON 文件 + 原子写入。

- 每次变更都整体落盘（写临时文件后 os.replace 原子替换），进程重启后数据仍在。
- 本层只负责存取，不理解业务规则。
- 并发安全：进程内由 service 层的锁保证；原子替换保证写到一半崩溃也不会损坏旧文件。
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Optional

from .models import Shift, Swap


class JsonFileStorage:
    def __init__(self, path: str):
        self.path = path
        self.shifts: dict[int, Shift] = {}
        self.swaps: dict[int, Swap] = {}
        self.next_shift_id = 1
        self.next_swap_id = 1
        self.load()

    # ---------- 读 ----------

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        self.shifts = {
            int(k): Shift.from_dict(v) for k, v in payload.get("shifts", {}).items()
        }
        self.swaps = {
            int(k): Swap.from_dict(v) for k, v in payload.get("swaps", {}).items()
        }
        self.next_shift_id = int(payload.get("next_shift_id", 1))
        self.next_swap_id = int(payload.get("next_swap_id", 1))

    # ---------- 写（原子） ----------

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        payload = {
            "shifts": {str(k): v.to_dict() for k, v in self.shifts.items()},
            "swaps": {str(k): v.to_dict() for k, v in self.swaps.items()},
            "next_shift_id": self.next_shift_id,
            "next_swap_id": self.next_swap_id,
        }
        # 同目录临时文件 + replace，保证原子性与崩溃后文件完整
        fd, tmp_path = tempfile.mkstemp(
            prefix=".nurse_swap-", suffix=".json.tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ---------- 班次 ----------

    def add_shift(self, shift: Shift) -> Shift:
        self.shifts[shift.id] = shift
        self.save()
        return shift

    def update_shift(self, shift: Shift) -> Shift:
        self.shifts[shift.id] = shift
        self.save()
        return shift

    def get_shift(self, shift_id: int) -> Optional[Shift]:
        return self.shifts.get(int(shift_id))

    def all_shifts(self) -> list[Shift]:
        return [self.shifts[k] for k in sorted(self.shifts)]

    def shifts_of_nurse(self, nurse: str) -> list[Shift]:
        return [s for s in self.all_shifts() if s.nurse == nurse]

    # ---------- 换班 ----------

    def add_swap(self, swap: Swap) -> Swap:
        self.swaps[swap.id] = swap
        self.save()
        return swap

    def update_swap(self, swap: Swap) -> Swap:
        self.swaps[swap.id] = swap
        self.save()
        return swap

    def get_swap(self, swap_id: int) -> Optional[Swap]:
        return self.swaps.get(int(swap_id))

    def all_swaps(self) -> list[Swap]:
        return [self.swaps[k] for k in sorted(self.swaps)]

    def swaps_of_nurse(self, nurse: str) -> list[Swap]:
        """护士作为原护士或接替人参与的换班单。"""
        return [
            s
            for s in self.all_swaps()
            if s.original_nurse == nurse or s.target_nurse == nurse
        ]

    # ---------- ID ----------

    def issue_shift_id(self) -> int:
        value = self.next_shift_id
        self.next_shift_id += 1
        return value

    def issue_swap_id(self) -> int:
        value = self.next_swap_id
        self.next_swap_id += 1
        return value
