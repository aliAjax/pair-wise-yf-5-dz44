"""HTTP 入口测试: 完整换班流程、错误映射、重启后数据可查。"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime

from nurse_swap import Store, SwapService
from nurse_swap.api import make_server

NOW = datetime(2026, 9, 26, 8, 0, 0)


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "api.db")
        self._start_server()

    def tearDown(self):
        self._stop_server()
        self.tmp.cleanup()

    def _start_server(self):
        self.store = Store(self.db_path)
        service = SwapService(self.store, clock=lambda: NOW)
        self.httpd = make_server(service, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        self.store.close()

    def req(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def mkshift(self, nurse="nurse-a", day="2026-09-27", start="08:00", end="16:00"):
        status, shift = self.req("POST", "/shifts", {
            "nurse_id": nurse, "date": day, "start_time": start, "end_time": end,
        })
        assert status == 201, shift
        return shift

    # ---------------- 完整流程 ----------------

    def test_full_swap_flow_over_http(self):
        shift = self.mkshift()

        status, swap = self.req("POST", "/swaps", {"shift_id": shift["id"], "nurse_id": "nurse-a"})
        self.assertEqual(status, 201)
        self.assertEqual(swap["state"], "pending")
        self.assertEqual(swap["shift"]["id"], shift["id"])

        status, swap = self.req("POST", f"/swaps/{swap['id']}/accept", {"nurse_id": "nurse-b"})
        self.assertEqual((status, swap["state"], swap["acceptor_id"]), (200, "accepted", "nurse-b"))

        # 顺序错误: 接替人不能先确认
        status, err = self.req("POST", f"/swaps/{swap['id']}/confirm", {"nurse_id": "nurse-b"})
        self.assertEqual(status, 409)

        status, swap = self.req("POST", f"/swaps/{swap['id']}/confirm", {"nurse_id": "nurse-a"})
        self.assertEqual((status, swap["state"], swap["requester_confirmed"]), (200, "accepted", True))

        status, swap = self.req("POST", f"/swaps/{swap['id']}/confirm", {"nurse_id": "nurse-b"})
        self.assertEqual((status, swap["state"]), (200, "completed"))

        # 班次归属已变更
        status, shifts = self.req("GET", "/nurses/nurse-b/shifts")
        self.assertEqual([s["id"] for s in shifts], [shift["id"]])

        # 按护士 + 状态查询
        status, swaps = self.req("GET", "/nurses/nurse-a/swaps?state=completed")
        self.assertEqual([s["id"] for s in swaps], [swap["id"]])
        status, swaps = self.req("GET", "/nurses/nurse-b/swaps?state=pending")
        self.assertEqual(swaps, [])

    def test_withdraw_then_reaccept_over_http(self):
        shift = self.mkshift()
        _, swap = self.req("POST", "/swaps", {"shift_id": shift["id"], "nurse_id": "nurse-a"})
        self.req("POST", f"/swaps/{swap['id']}/accept", {"nurse_id": "nurse-b"})
        self.req("POST", f"/swaps/{swap['id']}/confirm", {"nurse_id": "nurse-a"})

        status, swap = self.req("POST", f"/swaps/{swap['id']}/withdraw", {"nurse_id": "nurse-a"})
        self.assertEqual((status, swap["state"]), (200, "pending"))
        self.assertIsNone(swap["acceptor_id"])
        self.assertFalse(swap["requester_confirmed"])  # 前一人的确认作废

        status, swap = self.req("POST", f"/swaps/{swap['id']}/accept", {"nurse_id": "nurse-c"})
        self.assertEqual((status, swap["acceptor_id"]), (200, "nurse-c"))

    # ---------------- 拒绝条件 ----------------

    def test_reject_overlapping_acceptor(self):
        self.mkshift(nurse="nurse-b", start="12:00", end="20:00")
        shift = self.mkshift()
        _, swap = self.req("POST", "/swaps", {"shift_id": shift["id"], "nurse_id": "nurse-a"})
        status, err = self.req("POST", f"/swaps/{swap['id']}/accept", {"nurse_id": "nurse-b"})
        self.assertEqual(status, 409)
        self.assertIn("重叠", err["error"])

    def test_reject_started_shift(self):
        shift = self.mkshift(day="2026-09-26", start="07:00", end="15:00")
        status, err = self.req("POST", "/swaps", {"shift_id": shift["id"], "nurse_id": "nurse-a"})
        self.assertEqual(status, 409)
        self.assertIn("已开始", err["error"])

    def test_reject_duplicate_open_request(self):
        shift = self.mkshift()
        self.req("POST", "/swaps", {"shift_id": shift["id"], "nurse_id": "nurse-a"})
        status, err = self.req("POST", "/swaps", {"shift_id": shift["id"], "nurse_id": "nurse-a"})
        self.assertEqual(status, 409)
        self.assertIn("未处理", err["error"])

    # ---------------- 错误映射 ----------------

    def test_error_mapping(self):
        status, _ = self.req("GET", "/swaps/nope")
        self.assertEqual(status, 404)                       # 资源不存在
        status, _ = self.req("POST", "/swaps", {"shift_id": "x", "nurse_id": "nurse-a"})
        self.assertEqual(status, 404)
        status, _ = self.req("POST", "/shifts", {"nurse_id": "nurse-a"})
        self.assertEqual(status, 400)                       # 缺字段
        status, _ = self.req("POST", "/shifts", {
            "nurse_id": "nurse-a", "date": "2026-09-27",
            "start_time": "16:00", "end_time": "08:00",
        })
        self.assertEqual(status, 409)                       # 起止时间非法
        status, _ = self.req("GET", "/nurses/nurse-a/swaps?state=bogus")
        self.assertEqual(status, 400)                       # 非法状态过滤
        status, _ = self.req("GET", "/unknown")
        self.assertEqual(status, 404)                       # 未知路由

    # ---------------- 重启持久化 ----------------

    def test_restart_keeps_all_states_queryable(self):
        # 待处理
        s1 = self.mkshift()
        _, r1 = self.req("POST", "/swaps", {"shift_id": s1["id"], "nurse_id": "nurse-a"})
        # 待交接
        s2 = self.mkshift(start="16:00", end="23:00")
        _, r2 = self.req("POST", "/swaps", {"shift_id": s2["id"], "nurse_id": "nurse-a"})
        self.req("POST", f"/swaps/{r2['id']}/accept", {"nurse_id": "nurse-b"})
        # 已完成
        s3 = self.mkshift(day="2026-09-28")
        _, r3 = self.req("POST", "/swaps", {"shift_id": s3["id"], "nurse_id": "nurse-a"})
        self.req("POST", f"/swaps/{r3['id']}/accept", {"nurse_id": "nurse-b"})
        self.req("POST", f"/swaps/{r3['id']}/confirm", {"nurse_id": "nurse-a"})
        self.req("POST", f"/swaps/{r3['id']}/confirm", {"nurse_id": "nurse-b"})

        # 模拟重启: 关掉服务, 用同一数据库文件重新拉起
        self._stop_server()
        self._start_server()

        status, swaps = self.req("GET", "/nurses/nurse-a/swaps")
        self.assertEqual(status, 200)
        states = {s["id"]: s["state"] for s in swaps}
        self.assertEqual(states, {r1["id"]: "pending", r2["id"]: "accepted", r3["id"]: "completed"})

        for state, expect in (("pending", r1), ("accepted", r2), ("completed", r3)):
            status, swaps = self.req("GET", f"/nurses/nurse-a/swaps?state={state}")
            self.assertEqual([s["id"] for s in swaps], [expect["id"]])

        # 接替人视角与班次归属变更同样在重启后保留
        status, swaps = self.req("GET", "/nurses/nurse-b/swaps?state=completed")
        self.assertEqual([s["id"] for s in swaps], [r3["id"]])
        status, shifts = self.req("GET", "/nurses/nurse-b/shifts")
        self.assertIn(s3["id"], [s["id"] for s in shifts])


if __name__ == "__main__":
    unittest.main()
