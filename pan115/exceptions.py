"""异常体系。

设计原则：
- NetworkError        —— 传输层故障（连接失败/超时/截断），可重试
- RateLimitError      —— 服务端限流（HTTP 429），应按 Retry-After 退避
- AuthError           —— 认证授权失败（token 无效且刷新失败 / 授权流程失败），不可自动恢复
- APIError            —— 业务层失败（state=false 且非 401 系错误码），确定性错误，不重试
- ObjectNotFoundError —— 目标文件(夹)不存在（GET /open/folder/get_info 返回空数组等）
- UploadError         —— 上传流程失败
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class Pan115Error(Exception):
    """所有 pan115 异常的基类。"""

    #: 是否建议重试
    retryable: bool = False


class NetworkError(Pan115Error):
    """网络传输失败（连接、超时、响应截断）。可重试。"""

    retryable = True


class RateLimitError(Pan115Error):
    """服务端限流（HTTP 429）。可重试，应尊重 Retry-After。"""

    retryable = True

    def __init__(self, message: str = "", retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class AuthError(Pan115Error):
    """认证授权失败。需要重新走授权流程。"""

    retryable = False


class APIError(Pan115Error):
    """业务接口返回失败（state=false / code != 0 且非授权问题）。"""

    retryable = False

    def __init__(self, code: Any, message: str, data: Optional[Dict[str, Any]] = None):
        self.code = code
        self.message = message
        self.data = data or {}
        super().__init__(f"[code={code}] {message}")


class ObjectNotFoundError(APIError):
    """目标对象不存在。"""

    def __init__(self, message: str = "对象不存在"):
        super().__init__(code="NOT_FOUND", message=message)


class UploadError(Pan115Error):
    """上传流程失败（初始化、签名校验、OSS 传输、回调确认）。"""

    retryable = False
