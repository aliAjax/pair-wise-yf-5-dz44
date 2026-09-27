"""HTTP 请求入口：路由、请求体解析、JSON 响应与错误码映射。

只做协议相关的事，业务规则全部在 rules/service 中。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .errors import ApiError
from .rules import RuleValidationError
from .service import NurseSwapService


class SwapRequestHandler(BaseHTTPRequestHandler):
    server_version = "NurseSwap/1.0"

    # service 由 build_server 注入到 server 实例上
    @property
    def service(self) -> NurseSwapService:
        return self.server.service  # type: ignore[attr-defined]

    def _split_target(self):
        """解析请求目标，兼容未编码的 UTF-8 查询参数（如 ?nurse=王敏）。

        BaseHTTPRequestHandler 按 latin-1 解码请求行，先还原成原始字节，
        再按 UTF-8 解码；百分号编码的参数由 parse_qs 负责解码。
        """
        raw = self.path.encode("latin-1", errors="replace")
        try:
            target = raw.decode("utf-8")
        except UnicodeDecodeError:
            target = raw.decode("latin-1")
        parts = urlsplit(target)
        query = parse_qs(parts.query, keep_blank_values=True)
        path = parts.path.rstrip("/") or "/"
        return path, query

    # ---------- 协议辅助 ----------

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "invalid_json", "请求体必须是合法 JSON") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_request", "请求体必须是 JSON 对象")
        return data

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, ApiError):
            self._send_json(exc.status, exc.to_dict())
        elif isinstance(exc, RuleValidationError):
            self._send_json(
                400, {"error": {"code": "invalid_request", "message": str(exc)}}
            )
        else:
            self.log_error("unhandled error: %r", exc)
            self._send_json(
                500, {"error": {"code": "internal_error", "message": "服务器内部错误"}}
            )

    def log_message(self, fmt, *args):  # 安静一点，避免污染测试输出
        if self.server.verbose_logging:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        try:
            path, query = self._split_target()

            if path == "/health":
                self._send_json(200, {"status": "ok"})
                return
            if path == "/api/shifts":
                nurse = query.get("nurse", [None])[0]
                shifts = self.service.list_shifts(nurse=nurse)
                self._send_json(200, {"shifts": [s.to_dict() for s in shifts]})
                return
            if path.startswith("/api/shifts/"):
                shift_id = path.rsplit("/", 1)[1]
                shift = self.service.get_shift_or_404(shift_id)
                self._send_json(200, {"shift": shift.to_dict()})
                return
            if path == "/api/swaps":
                nurse = query.get("nurse", [None])[0]
                status = query.get("status", [None])[0]
                swaps = self.service.list_swaps(nurse=nurse, status=status)
                self._send_json(200, {"swaps": [s.to_dict() for s in swaps]})
                return
            if path.startswith("/api/swaps/"):
                rest = path[len("/api/swaps/") :].split("/")
                if len(rest) == 1:
                    swap = self.service.get_swap(rest[0])
                    self._send_json(200, {"swap": swap.to_dict()})
                    return
                self._method_not_allowed_or_404(rest[1])
                return

            self._send_json(404, {"error": {"code": "not_found", "message": "路径不存在"}})
        except Exception as exc:  # noqa: BLE001 - 统一错误出口
            self._handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path, _ = self._split_target()
            # 容忍未百分号编码的非 ASCII 查询参数（如 ?nurse=王敏）
            data = self._read_json()

            if path == "/api/shifts":
                shift = self.service.create_shift(
                    nurse=data.get("nurse"),
                    date_str=data.get("date"),
                    start_time=data.get("start_time"),
                    end_time=data.get("end_time"),
                )
                self._send_json(201, {"shift": shift.to_dict()})
                return

            if path == "/api/swaps":
                swap = self.service.submit_swap(
                    shift_id=data.get("shift_id"),
                    original_nurse=data.get("original_nurse"),
                    target_nurse=data.get("target_nurse"),
                )
                self._send_json(201, {"swap": swap.to_dict()})
                return

            if path.startswith("/api/swaps/"):
                rest = path[len("/api/swaps/") :].split("/")
                if len(rest) == 2:
                    swap_id, action = rest
                    nurse = data.get("nurse")
                    if action == "accept":
                        swap = self.service.accept_swap(swap_id, nurse)
                    elif action == "confirm":
                        swap = self.service.confirm_swap(swap_id, nurse)
                    elif action == "withdraw":
                        swap = self.service.withdraw_swap(swap_id, nurse)
                    else:
                        self._method_not_allowed_or_404(action)
                        return
                    self._send_json(200, {"swap": swap.to_dict()})
                    return

            self._send_json(404, {"error": {"code": "not_found", "message": "路径不存在"}})
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)

    def _method_not_allowed_or_404(self, token: str) -> None:
        self._send_json(
            404, {"error": {"code": "not_found", "message": f"未知操作: {token}"}}
        )


def build_server(host: str, port: int, service: NurseSwapService, verbose: bool = False):
    server = ThreadingHTTPServer((host, port), SwapRequestHandler)
    server.service = service  # type: ignore[attr-defined]
    server.verbose_logging = verbose  # type: ignore[attr-defined]
    return server
