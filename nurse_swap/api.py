"""HTTP 请求入口: 仅负责解析请求、调用服务层、组装响应, 不含业务规则。"""
from __future__ import annotations

import json
import re
from datetime import date, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .errors import DomainError, NotFoundError
from .models import SwapState
from .service import SwapService
from .storage import Store


class _BadRequest(Exception):
    """请求格式错误, 映射为 400。"""


def make_server(service: SwapService, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _make_handler(service))


def run(host: str, port: int, db_path: str) -> None:
    store = Store(db_path)
    service = SwapService(store)
    httpd = make_server(service, host, port)
    print(f"护士换班服务已启动: http://{host}:{port} (数据库: {db_path})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()


def _make_handler(service: SwapService):
    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "NurseSwap/1.0"
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def log_message(self, fmt, *args):  # 保持控制台干净
            pass

        # ---------------- 分发 ----------------

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)
            try:
                status, payload = self._route(method, path, query)
            except _BadRequest as exc:
                status, payload = 400, {"error": str(exc)}
            except DomainError as exc:
                status, payload = exc.status, {"error": str(exc)}
            except Exception as exc:  # pragma: no cover - 兜底
                status, payload = 500, {"error": f"内部错误: {exc}"}
            self._send_json(status, payload)

        def _route(self, method: str, path: str, query: dict) -> tuple[int, object]:
            # 创建班次
            if method == "POST" and path == "/shifts":
                body = self._read_json()
                shift = service.create_shift(
                    nurse_id=_required_str(body, "nurse_id"),
                    day=_parse_date(_required_str(body, "date")),
                    start_time=_parse_time(_required_str(body, "start_time")),
                    end_time=_parse_time(_required_str(body, "end_time")),
                )
                return 201, shift.to_dict()

            # 按护士查询班次
            m = re.fullmatch(r"/nurses/([^/]+)/shifts", path)
            if method == "GET" and m:
                shifts = service.list_nurse_shifts(m.group(1))
                return 200, [s.to_dict() for s in shifts]

            # 提交换班
            if method == "POST" and path == "/swaps":
                body = self._read_json()
                request = service.submit_swap(
                    shift_id=_required_str(body, "shift_id"),
                    requester_id=_required_str(body, "nurse_id"),
                )
                return 201, self._swap_view(request.id)

            # 查询单个换班申请
            m = re.fullmatch(r"/swaps/([^/]+)", path)
            if method == "GET" and m:
                return 200, self._swap_view(m.group(1))

            # 接替 / 确认 / 撤回
            m = re.fullmatch(r"/swaps/([^/]+)/(accept|confirm|withdraw)", path)
            if method == "POST" and m:
                swap_id, action = m.group(1), m.group(2)
                nurse_id = _required_str(self._read_json(), "nurse_id")
                if action == "accept":
                    request = service.accept_swap(swap_id, nurse_id)
                elif action == "confirm":
                    request = service.confirm_swap(swap_id, nurse_id)
                else:
                    request = service.withdraw_swap(swap_id, nurse_id)
                return 200, self._swap_view(request.id)

            # 按护士查询换班申请, 可用 ?state=pending|accepted|completed 过滤
            m = re.fullmatch(r"/nurses/([^/]+)/swaps", path)
            if method == "GET" and m:
                state = _parse_state(query)
                items = service.list_nurse_swaps(m.group(1), state)
                return 200, [_swap_dict(req, shift) for req, shift in items]

            raise NotFoundError(f"接口不存在: {method} {path}")

        # ---------------- 辅助 ----------------

        def _swap_view(self, request_id: str) -> dict:
            request, shift = service.get_swap(request_id)
            return _swap_dict(request, shift)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise _BadRequest("请求体不是合法 JSON")
            if not isinstance(body, dict):
                raise _BadRequest("请求体应为 JSON 对象")
            return body

        def _send_json(self, status: int, payload: object) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ApiHandler


def _swap_dict(request, shift) -> dict:
    data = request.to_dict()
    data["shift"] = shift.to_dict()
    return data


def _required_str(body: dict, field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _BadRequest(f"缺少必填字段: {field}")
    return value.strip()


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise _BadRequest(f"date 格式应为 YYYY-MM-DD, 收到: {raw!r}")


def _parse_time(raw: str) -> time:
    try:
        parsed = time.fromisoformat(raw)
    except ValueError:
        raise _BadRequest(f"时间格式应为 HH:MM, 收到: {raw!r}")
    return parsed.replace(second=0, microsecond=0)


def _parse_state(query: dict) -> SwapState | None:
    raw = query.get("state", [None])[0]
    if raw is None:
        return None
    try:
        return SwapState(raw)
    except ValueError:
        allowed = ", ".join(s.value for s in SwapState)
        raise _BadRequest(f"state 只能是: {allowed}")
