"""HTTP 端到端：真实端口 + http.client。"""

import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from nurse_swap.service import NurseSwapService
from nurse_swap.storage import JsonFileStorage
from nurse_swap.webapp import build_server

DAY = "2026-11-01"


def request(base, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clock = lambda: datetime(2020, 1, 1)  # noqa: E731
        service = NurseSwapService(
            JsonFileStorage(str(Path(self.tmp.name) / "http.json")), clock=clock
        )
        self.httpd = build_server("127.0.0.1", 0, service)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def test_health(self):
        status, body = request(self.base, "GET", "/health")
        self.assertEqual((status, body), (200, {"status": "ok"}))

    def test_full_flow(self):
        status, body = request(
            self.base, "POST", "/api/shifts",
            {"nurse": "Alice", "date": DAY, "start_time": "08:00", "end_time": "12:00"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["shift"]["id"], 1)
        request(
            self.base, "POST", "/api/shifts",
            {"nurse": "Bob", "date": DAY, "start_time": "13:00", "end_time": "18:00"},
        )

        status, body = request(
            self.base, "POST", "/api/swaps",
            {"shift_id": 1, "original_nurse": "Alice", "target_nurse": "Bob"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["swap"]["status"], "pending")

        status, body = request(self.base, "POST", "/api/swaps/1/accept", {"nurse": "Bob"})
        self.assertEqual(status, 200)
        self.assertEqual(body["swap"]["status"], "handover")

        # 接替人抢先确认 -> 403
        status, body = request(self.base, "POST", "/api/swaps/1/confirm", {"nurse": "Bob"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

        # 原护士第一次确认
        status, body = request(self.base, "POST", "/api/swaps/1/confirm", {"nurse": "Alice"})
        self.assertEqual(body["swap"]["confirm_stage"], "awaiting_target")

        # 第二次确认前撤回，确认作废
        status, body = request(self.base, "POST", "/api/swaps/1/withdraw", {"nurse": "Alice"})
        self.assertEqual(body["swap"]["confirm_stage"], "awaiting_original")

        # 重新依次确认 -> 完成
        request(self.base, "POST", "/api/swaps/1/confirm", {"nurse": "Alice"})
        status, body = request(self.base, "POST", "/api/swaps/1/confirm", {"nurse": "Bob"})
        self.assertEqual(body["swap"]["status"], "completed")

        # 班次转交
        status, body = request(self.base, "GET", "/api/shifts/1")
        self.assertEqual(body["shift"]["nurse"], "Bob")

        # 按护士查询
        status, body = request(self.base, "GET", "/api/swaps?nurse=Bob")
        self.assertEqual(len(body["swaps"]), 1)
        status, body = request(self.base, "GET", "/api/shifts?nurse=Bob")
        self.assertEqual(len(body["shifts"]), 2)

    def test_rejections(self):
        request(
            self.base, "POST", "/api/shifts",
            {"nurse": "Alice", "date": DAY, "start_time": "08:00", "end_time": "12:00"},
        )
        request(
            self.base, "POST", "/api/shifts",
            {"nurse": "Bob", "date": DAY, "start_time": "11:00", "end_time": "13:00"},
        )
        status, body = request(
            self.base, "POST", "/api/swaps",
            {"shift_id": 1, "original_nurse": "Alice", "target_nurse": "Bob"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "target_schedule_conflict")

        status, _ = request(
            self.base, "POST", "/api/swaps",
            {"shift_id": 1, "original_nurse": "X", "target_nurse": "Bob"},
        )
        self.assertEqual(status, 403)

        self.assertEqual(request(self.base, "GET", "/api/swaps/99")[0], 404)
        self.assertEqual(request(self.base, "GET", "/api/shifts/99")[0], 404)

        # 非法 JSON
        req = urllib.request.Request(
            self.base + "/api/shifts",
            data=b"{not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)

    def test_bad_input(self):
        status, body = request(
            self.base, "POST", "/api/shifts",
            {"nurse": "Alice", "date": DAY, "start_time": "18:00", "end_time": "08:00"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

    def test_nonascii_query_param_raw_and_encoded(self):
        # JSON 请求体中的中文护士名
        request(
            self.base, "POST", "/api/shifts",
            {"nurse": "王敏", "date": DAY, "start_time": "08:00", "end_time": "12:00"},
        )
        # 未百分号编码的原始 UTF-8 查询参数（模拟 curl 直接发原始字节）
        host, port = self.httpd.server_address
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(
                "GET /api/shifts?nurse=王敏 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode("utf-8")
            )
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        raw = b"".join(chunks)
        self.assertIn(b"200", raw.split(b"\r\n", 1)[0])
        body = json.loads(raw.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
        self.assertEqual(len(body["shifts"]), 1)

        # 百分号编码形式
        status, body = request(
            self.base, "GET", "/api/shifts?nurse=%E7%8E%8B%E6%95%8F"
        )
        self.assertEqual(len(body["shifts"]), 1)


if __name__ == "__main__":
    unittest.main()
