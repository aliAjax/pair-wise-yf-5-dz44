"""业务异常：携带 HTTP 状态码与机器可读错误码。"""


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}
