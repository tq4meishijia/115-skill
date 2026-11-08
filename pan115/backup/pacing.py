"""节奏 / 退避 / 频控恢复：纯决策逻辑，不发起网络请求，可离线重放。

三块内容：
- ``Pacer``      —— 唯一节拍器，TokenBucket 兼容接口（acquire），**一次等待**；
                    令牌桶硬地板与最小间隔取 max，绝不串联累加（需求 v0.2 §10.1）。
- ``RetryPolicy``—— 可重试分类 + 双档退避数学（strict / extended）。
- ``ThrottleFSM``—— COOLDOWN / PROBE / RAMP / TRIPPED 纯状态机。

官方口径：115 开放平台未公开任何 QPS 数值，也未定义 HTTP 429 / Retry-After。
本模块所有数值是「工程假设 + 社区实测参考」，非官方承诺。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from ..exceptions import APIError, AuthError, NetworkError, RateLimitError
from ..ratelimit import TokenBucket

# 频控分类结果
THROTTLE_HTTP = "throttle_http"      # HTTP 429 / RateLimitError
THROTTLE_BIZ = "throttle_biz"        # 业务码 770004 / 406
AUTH = "auth"
RETRYABLE = "retryable"              # 网络故障 / 5xx
DETERMINISTIC = "deterministic"      # 404 重名、参数错误等
UNKNOWN = "unknown"                  # 未列入码表：只 warning，不自动进 COOLDOWN

RETRYABLE_HTTP_CODES = (500, 502, 503, 504)


def classify_error(exc: BaseException, throttle_business_codes: Tuple[int, ...]) -> str:
    """把异常映射到频控分类。未知码只给 UNKNOWN，不自动当作限流（灰区裁决）。"""
    if isinstance(exc, AuthError):
        return AUTH
    if isinstance(exc, RateLimitError):
        return THROTTLE_HTTP
    if isinstance(exc, NetworkError):
        return RETRYABLE
    if isinstance(exc, APIError):
        code = exc.code
        try:
            icode = int(code)
        except (TypeError, ValueError):
            icode = None
        if icode in throttle_business_codes:
            return THROTTLE_BIZ
        if icode == 429:
            return THROTTLE_HTTP
        if icode in RETRYABLE_HTTP_CODES:
            return RETRYABLE
        if icode is None or isinstance(code, str):
            return UNKNOWN
        return DETERMINISTIC
    if isinstance(exc, OSError):
        return RETRYABLE
    return UNKNOWN


# ----------------------------------------------------------------------
# Pacer：唯一节拍器
# ----------------------------------------------------------------------
class Pacer(TokenBucket):
    """替换 ``OpenClient._limiter`` 的单一节拍器（继承现有限流类）。

    只覆盖 ``acquire()``：把「令牌桶硬地板」与「最小间隔地板」合并为**一次等待**，
    不串联累加（需求 v0.2 §10.1）。``try_acquire`` 等能力由父类提供。
    因 ``client.request`` 内部每次请求都会 ``self._limiter.acquire()``，换上本类后
    L1 重试、token 刷新回放、二次校验重发全部自动经过同一节拍器，无需改 client.py。

    ``ponytail:`` 一次 sleep；升级路径：需按接口分组限流时再拆多个 Pacer。
    """

    def __init__(self, settings: Any, *, clock=time.monotonic, sleep=time.sleep,
                 rng: Optional[random.Random] = None):
        super().__init__(float(settings.api_max_qps),
                         float(settings.token_bucket_capacity))
        self._s = settings
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._rate = float(settings.api_max_qps)          # <= 1.0，settings 已校验
        self._min_interval = float(settings.min_request_interval_ms) / 1000.0
        self._jitter_ratio = float(settings.jitter_ratio)
        self._cap = float(settings.jitter_cap_ms) / 1000.0
        self._last: Optional[float] = None
        self._rate_cap = 1.0
        self._stamps: List[float] = []

    # -- 供 RAMP 调速（永远 <= 1.0）--
    def set_rate_cap(self, qps: float) -> None:
        self._rate_cap = min(float(qps), 1.0)

    @property
    def rate_cap(self) -> float:
        return self._rate_cap

    def _interval(self) -> float:
        """有效间隔：max(1/速率上限, 最小间隔) 后追加 0~10% 抖动，单次计算。"""
        floor = 1.0 / min(self._rate_cap, self._rate)
        base = max(self._min_interval, floor)
        jittered = base * (1.0 + self._rng.uniform(0.0, self._jitter_ratio))
        # 正常态封顶 1200ms；RAMP 降速态（base >= 2s）不受此封顶影响。
        return min(jittered, self._cap) if self._rate_cap >= 1.0 else jittered

    def acquire(self, tokens: float = 1.0, timeout: Optional[float] = None) -> None:
        """一次等待同时满足「令牌桶硬地板」与「最小间隔地板」。

        因 ``interval >= 1/rate`` 且 ``capacity=1``，等待结束后令牌必然已补满，
        故只做一次 ``sleep`` 并消费令牌，绝不产生第二次阻塞。
        """
        now = self._clock()
        interval = self._interval()
        target = (self._last if self._last is not None else now - interval) + interval
        wait = max(0.0, target - now)
        if wait > 0:
            self._sleep(wait)
        with self._lock:
            self._tokens = max(0.0, self.capacity - tokens)
            self._updated = now
        self._last = self._clock()
        self._stamps.append(self._last)

    @property
    def stamps(self) -> List[float]:
        return list(self._stamps)

    def peak_qps(self) -> float:
        """1 秒滑动窗口内最大请求数（离线可复算）。"""
        stamps = sorted(self._stamps)
        peak, j = 0, 0
        for i, t in enumerate(stamps):
            while t - stamps[j] >= 1.0:
                j += 1
            peak = max(peak, i - j + 1)
        return float(peak)


# ----------------------------------------------------------------------
# RetryPolicy：双档退避（纯函数）
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class RetryPolicy:
    profile: str = "strict"
    max_attempts: int = 3
    cap_s: float = 8.0
    retry_after_cap_s: float = 30.0
    jitter: float = 0.5
    base_s: float = 1.0
    factor: float = 2.0

    @classmethod
    def from_settings(cls, s: Any) -> "RetryPolicy":
        if s.retry_profile == "strict":
            # 复用现有 client.py 的语义：min(2**attempt, 8) + 0~0.5s 抖动，3 次。
            return cls(profile="strict", max_attempts=int(s.max_retries),
                       cap_s=8.0, retry_after_cap_s=float(s.retry_after_cap_s),
                       jitter=0.5, base_s=1.0, factor=2.0)
        return cls(profile="extended", max_attempts=int(s.retry_max_attempts),
                   cap_s=float(s.retry_cap_s),
                   retry_after_cap_s=float(s.retry_after_cap_s),
                   jitter=float(s.retry_jitter_ratio),
                   base_s=float(s.retry_base_s), factor=float(s.retry_factor))

    def delay(self, attempt: int, retry_after: Optional[float] = None,
              rng: Optional[random.Random] = None) -> Optional[float]:
        """返回下次重试前等待秒数；None 表示不再重试（确定性失败 / 次数耗尽）。"""
        if attempt >= self.max_attempts:
            return None
        r = rng or random.Random()
        if self.profile == "strict":
            raw = min(self.base_s * (self.factor ** attempt), self.cap_s)
            d = raw + r.uniform(0.0, self.jitter)
        else:
            raw = min(self.base_s * (self.factor ** attempt), self.cap_s)
            d = raw * (1.0 + r.uniform(-self.jitter, self.jitter))
        if retry_after is not None:
            d = min(max(d, float(retry_after)), self.retry_after_cap_s) if self.profile == "strict" \
                else max(d, float(retry_after))
        return max(0.0, d)


# ----------------------------------------------------------------------
# ThrottleFSM：纯决策，不发请求、不 sleep
# ----------------------------------------------------------------------
RUNNING, COOLDOWN, PROBE, RAMP, TRIPPED = "RUNNING", "COOLDOWN", "PROBE", "RAMP", "TRIPPED"


@dataclass(frozen=True)
class Decision:
    state: str
    wait_s: float = 0.0          # 进入该状态前需等待的秒数
    rate_cap: float = 1.0        # 该状态生效的请求速率上限
    trip: bool = False           # 是否熔断终止本轮
    probe: bool = False          # 是否发单发探测
    note: str = ""


@dataclass
class ThrottleFSM:
    cooldown_initial_s: float = 300.0
    cooldown_factor: float = 2.0
    cooldown_cap_s: float = 3600.0
    probe_count: int = 5
    ramp_stages: Tuple[float, ...] = (0.5, 0.7, 0.9, 1.0)
    ramp_min_requests: int = 20
    ramp_min_seconds: float = 60.0
    max_probe_fail_rounds: int = 3

    state: str = RUNNING
    cooldown_s: float = field(default=0.0)
    probe_ok_streak: int = 0
    fail_rounds: int = 0
    ramp_index: int = 0
    ramp_requests: int = 0
    ramp_seconds: float = 0.0

    @classmethod
    def from_settings(cls, s: Any) -> "ThrottleFSM":
        return cls(cooldown_initial_s=float(s.cooldown_initial_s),
                   cooldown_factor=float(s.cooldown_factor),
                   cooldown_cap_s=float(s.cooldown_cap_s),
                   probe_count=int(s.probe_count),
                   ramp_stages=tuple(float(x) for x in s.ramp_qps_stages),
                   ramp_min_requests=int(s.ramp_min_requests),
                   ramp_min_seconds=float(s.ramp_min_seconds),
                   max_probe_fail_rounds=int(s.max_probe_fail_rounds))

    # -- 事件迁移 --
    def on_throttle(self, retry_after: Optional[float] = None) -> Decision:
        """RUNNING -> COOLDOWN。Retry-After 只作地板，不会缩短冷却。"""
        base = self.cooldown_s if self.state != RUNNING else self.cooldown_initial_s
        if self.state != RUNNING:
            base = min(base * self.cooldown_factor, self.cooldown_cap_s)
        cd = min(max(base, float(retry_after or 0)), self.cooldown_cap_s)
        self.cooldown_s = cd
        self.state = COOLDOWN
        self.probe_ok_streak = 0
        return Decision(COOLDOWN, wait_s=cd, rate_cap=0.0,
                        note=f"cooldown={cd:.0f}s")

    def on_cooldown_done(self) -> Decision:
        """COOLDOWN -> PROBE（每轮单发 1 个探测请求）。"""
        self.state = PROBE
        return Decision(PROBE, wait_s=0.0, rate_cap=0.5, probe=True,
                        note="single probe")

    def on_probe_result(self, ok: bool) -> Decision:
        if not ok:
            self.fail_rounds += 1
            self.probe_ok_streak = 0
            if self.fail_rounds >= self.max_probe_fail_rounds:
                self.state = TRIPPED
                return Decision(TRIPPED, wait_s=0.0, rate_cap=0.0, trip=True,
                                note=f"probe_fail_rounds={self.fail_rounds}")
            return self.on_throttle()
        self.probe_ok_streak += 1
        if self.probe_ok_streak < self.probe_count:
            return Decision(PROBE, wait_s=0.0, rate_cap=0.5, probe=True,
                            note=f"probe {self.probe_ok_streak}/{self.probe_count}")
        self.state = RAMP
        self.ramp_index = 0
        self.ramp_requests = 0
        self.ramp_seconds = 0.0
        return Decision(RAMP, wait_s=0.0, rate_cap=self.ramp_stages[0],
                        note="probes all succeeded -> RAMP")

    def observe(self, requests: int, seconds: float) -> Decision:
        """RAMP 期间累计观察量，达标才升档；上限锁死 1.0 QPS。"""
        self.ramp_requests += requests
        self.ramp_seconds += seconds
        if (self.ramp_requests >= self.ramp_min_requests
                and self.ramp_seconds >= self.ramp_min_seconds):
            self.ramp_requests, self.ramp_seconds = 0, 0.0
            self.ramp_index = min(self.ramp_index + 1, len(self.ramp_stages) - 1)
            cap = min(self.ramp_stages[self.ramp_index], 1.0)
            if self.ramp_index == len(self.ramp_stages) - 1 and cap >= 1.0:
                self.state = RUNNING
                self.fail_rounds = 0
                self.cooldown_s = 0.0
                return Decision(RUNNING, wait_s=0.0, rate_cap=1.0, note="RAMP complete")
            return Decision(RAMP, wait_s=0.0, rate_cap=cap,
                            note=f"ramp -> {cap} QPS")
        cap = min(self.ramp_stages[self.ramp_index], 1.0)
        return Decision(RAMP, wait_s=0.0, rate_cap=cap, note="observing")

    def reset(self) -> None:
        self.state = RUNNING
        self.cooldown_s = 0.0
        self.probe_ok_streak = 0
        self.fail_rounds = 0
        self.ramp_index = 0
        self.ramp_requests = 0
        self.ramp_seconds = 0.0
