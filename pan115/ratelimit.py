"""客户端限速：令牌桶算法。

115 开放平台对 API 调用实施限流策略（参见开发者协议 5.10 条），
客户端侧主动限速可以避免触发服务端风控。默认 2 请求/秒。
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """线程安全的令牌桶。

    :param rate: 令牌生成速率（个/秒）
    :param capacity: 桶容量，默认为 rate（至少 1）
    """

    def __init__(self, rate: float, capacity: float = None):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = float(rate)
        self.capacity = max(float(capacity if capacity is not None else rate), 1.0)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0, timeout: float = None) -> None:
        """阻塞直到获取到令牌。timeout 为 None 表示无限等待。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self.rate
            if deadline is not None and time.monotonic() + wait > deadline:
                raise TimeoutError("rate limit acquire timeout")
            time.sleep(min(wait, 0.5))
