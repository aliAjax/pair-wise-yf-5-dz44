"""启动入口：python main.py [--host 127.0.0.1] [--port 8000] [--store data/nurse_swap.json]"""

from __future__ import annotations

import argparse
import os

from nurse_swap.service import NurseSwapService
from nurse_swap.storage import JsonFileStorage
from nurse_swap.webapp import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="护士换班服务")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", "8000"))
    )
    parser.add_argument(
        "--store",
        default=os.environ.get("STORE_PATH", "data/nurse_swap.json"),
        help="JSON 持久化文件路径，重启后从此文件恢复",
    )
    parser.add_argument("--verbose", action="store_true", help="打印 HTTP 访问日志")
    args = parser.parse_args()

    storage = JsonFileStorage(args.store)
    service = NurseSwapService(storage)
    server = build_server(args.host, args.port, service, verbose=args.verbose)

    print(
        f"护士换班服务已启动: http://{args.host}:{args.port}  (数据文件: {args.store})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
