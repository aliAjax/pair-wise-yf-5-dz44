"""启动入口: python3 main.py

环境变量:
  HOST          监听地址, 默认 127.0.0.1
  PORT          监听端口, 默认 8000
  NURSE_SWAP_DB SQLite 数据库文件路径, 默认 nurse_swap.db
"""
import os

from nurse_swap.api import run

if __name__ == "__main__":
    run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        db_path=os.environ.get("NURSE_SWAP_DB", "nurse_swap.db"),
    )
