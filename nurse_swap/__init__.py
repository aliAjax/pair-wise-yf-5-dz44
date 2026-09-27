"""护士换班服务。

分层：
- models:  数据模型
- rules:   纯业务规则（无 IO，可独立测试）
- storage: 持久化（JSON 文件，原子写入，重启可恢复）
- service: 业务编排（事务性地调用 rules + storage）
- webapp:  HTTP 请求入口（路由 / JSON / 状态码）
"""

from .service import NurseSwapService
from .storage import JsonFileStorage

__all__ = ["NurseSwapService", "JsonFileStorage"]
