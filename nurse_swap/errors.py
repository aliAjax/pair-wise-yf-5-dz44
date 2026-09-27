"""领域错误: 由规则层/服务层抛出, HTTP 层按 status 映射为响应。"""


class DomainError(Exception):
    """业务错误基类。"""

    status = 400


class NotFoundError(DomainError):
    """资源不存在。"""

    status = 404


class RuleViolation(DomainError):
    """违反业务规则(如班次重叠、班次已开始、已有未处理换班等)。"""

    status = 409
