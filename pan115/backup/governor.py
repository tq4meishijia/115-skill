"""所有 ``/open/`` 请求的唯一网关（零改动 client.py）。

装到现有 ``OpenClient`` 上的三件事：
1. ``client._limiter = Pacer`` —— 复用 client.py 内部已有的 ``_limiter.acquire()``
   调用点，让 L1 重试、token 刷新与回放、二次校验重发全部经同一节拍器；
2. ``client.on_request / on_retry`` 钩子 —— 精确统计 ``N_api``（含内部重试）
   并写结构化事件；
3. ``config.rate_limit = 1.0`` 强制覆盖，``PAN115_RATE_LIMIT`` 由入口显式设置。

业务层在 governor 这里补齐：业务码 770004/406 识别（不改 client.py）、
频控恢复状态机、strict/extended 双档重试。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from ..exceptions import AuthError
from .pacing import (
    DETERMINISTIC, RETRYABLE, THROTTLE_BIZ, THROTTLE_HTTP, UNKNOWN,
    Pacer, RetryPolicy, ThrottleFSM, classify_error,
)


class BackupTripped(Exception):
    """连续探测失败 -> 本轮熔断，退出码 3。"""


class ThrottleSignal(Exception):
    """捕获到频控信号；本轮立即停止业务请求，交 recover() 处理。"""

    def __init__(self, source: str, retry_after: Optional[float] = None, detail: str = ""):
        super().__init__(f"频控信号 {source}: {detail}")
        self.source = source
        self.retry_after = retry_after
        self.detail = detail


class Governor:
    """duck-type 兼容 OpenClient：可传给 FsAPI / UploadAPI / OssUploader。"""

    def __init__(self, client: Any, settings: Any, reporter: Any, *,
                 pacer: Pacer = None, fsm: ThrottleFSM = None,
                 retry_policy: RetryPolicy = None,
                 sleeper=time.sleep, clock=time.monotonic):
        self._client = client
        self.s = settings
        self.reporter = reporter
        self.pacer = pacer or Pacer(settings)
        self.fsm = fsm or ThrottleFSM.from_settings(settings)
        self.policy = retry_policy or RetryPolicy.from_settings(settings)
        self._sleeper = sleeper
        self._clock = clock
        self.n_api = 0
        self.throttle_events = {"http429": 0, "code_770004": 0, "code_406": 0, "total": 0}
        self.retry_exhausted = 0
        self.unknown_warnings: list = []
        self._codes = tuple(int(c) for c in settings.throttle_business_codes)
        self._last_ts = clock()

    # -- 兼容 OpenClient 的最小接口 --
    @property
    def config(self) -> Any:
        return self._client.config

    def install(self) -> None:
        """把唯一节拍器与观测钩子挂到现有 client（不改其源码）。"""
        c = self._client
        c.config.rate_limit = float(self.s.api_max_qps)   # 强制覆盖默认 2.0
        c.config.timeout = float(self.s.timeout_s)
        c.config.upload_part_size = int(self.s.upload_part_size)
        c.config.page_size = int(self.s.page_size)
        if self.s.retry_profile == "strict":
            c.config.max_retries = int(self.s.max_retries)
        else:
            # extended 档由 governor 独占重试，禁用 client 内置 L1 避免次数叠乘
            c.config.max_retries = 0
        c._limiter = self.pacer
        c.on_request = self._on_request
        c.on_retry = self._on_retry
        c.on_refresh = self._on_refresh

    def _on_request(self, method: str, url: str) -> None:
        self.n_api += 1
        self.reporter.log("api_request", request_type=method, endpoint=url,
                          api_index=self.n_api)

    def _on_retry(self, err: BaseException, attempt: int, delay: float) -> None:
        self.reporter.log("retry", attempt=attempt + 1, delay_s=delay,
                          error_type=type(err).__name__)

    def _on_refresh(self) -> None:
        self.reporter.log("auth_required", event_detail="token_refreshed",
                          needs_manual_action="")

    # -- 唯一请求入口 --
    def request(self, method: str, path: str, **kw) -> Dict[str, Any]:
        if self.fsm.state == "TRIPPED":
            raise BackupTripped("频控恢复连续失败，本轮已熔断")
        if self.fsm.state == "RAMP":
            self.pacer.set_rate_cap(self.fsm.ramp_stages[self.fsm.ramp_index])
        elif self.fsm.state == "PROBE":
            self.pacer.set_rate_cap(float(self.s.probe_qps))

        attempts = self.policy.max_attempts if self.s.retry_profile == "extended" else 1
        attempt = 0
        while True:
            try:
                body = self._client.request(method, path, **kw)
            except BaseException as e:
                kind = classify_error(e, self._codes)
                if kind == "auth":
                    raise
                if kind in (THROTTLE_HTTP, THROTTLE_BIZ):
                    src = "http429" if kind == THROTTLE_HTTP else f"code_{getattr(e, 'code', 0)}"
                    self._record_throttle(src)
                    raise ThrottleSignal(
                        src, getattr(e, "retry_after", None), f"{type(e).__name__}: {e}"
                    ) from e
                if kind == RETRYABLE and self.s.retry_profile == "extended" and attempt + 1 < attempts:
                    d = self.policy.delay(attempt)
                    self.reporter.log("retry", attempt=attempt + 1, delay_s=d,
                                      error_type=type(e).__name__)
                    if d:
                        self._sleeper(d)
                    attempt += 1
                    continue
                if kind == UNKNOWN:
                    self.unknown_warnings.append(
                        {"type": type(e).__name__, "code": str(getattr(e, "code", "")),
                         "detail": str(e)[:200]}
                    )
                    self.reporter.log("api_request", event_detail="UNKNOWN_WARNING",
                                      error_type=type(e).__name__)
                if kind in (RETRYABLE, DETERMINISTIC, UNKNOWN):
                    self.retry_exhausted += 1
                raise
            # 成功：RAMP 观察窗累计（升档后才回到 RUNNING）
            now = self._clock()
            elapsed = max(0.0, now - self._last_ts)
            self._last_ts = now
            if self.fsm.state == "RAMP":
                d = self.fsm.observe(1, elapsed)
                self.pacer.set_rate_cap(d.rate_cap)
            return body

    def _record_throttle(self, source: str) -> None:
        key = "http429" if source == "http429" else (
            "code_770004" if "770004" in source else
            "code_406" if "406" in source else "http429"
        )
        self.throttle_events[key] += 1
        self.throttle_events["total"] += 1
        self.reporter.log("rate_limited", source=source, fsm_state=self.fsm.state)

    # -- 频控恢复（可注入 sleeper，离线自检不真等 300s）--
    def _probe_once(self) -> bool:
        try:
            # 单发探测；经 client.request -> pacer（0.5 QPS）-> on_request 计入 N_api
            self._client.request("GET", self.s.probe_path)
            return True
        except BaseException:
            return False

    def recover(self, signal: ThrottleSignal) -> str:
        """返回 ``recovered`` / ``tripped``。冷却期内 0 次重试。"""
        d = self.fsm.on_throttle(signal.retry_after)
        self.reporter.log("rate_limited", event_detail="cooldown_start",
                          wait_s=d.wait_s, source=signal.source)
        while True:
            if d.wait_s > 0:
                self._sleeper(d.wait_s)
                d = self.fsm.on_cooldown_done()
            if d.trip:
                raise BackupTripped(
                    f"连续 {self.fsm.fail_rounds} 轮探测失败，本轮熔断，等下一调度周期"
                )
            if d.probe:
                ok = self._probe_once()
                d = self.fsm.on_probe_result(ok)
                self.reporter.log("api_request", event_detail="probe_result",
                                  ok=ok, fsm_state=d.state)
                continue
            if d.state == "RAMP":
                self.pacer.set_rate_cap(d.rate_cap)
                return "recovered"
            if d.state == "RUNNING":
                self.pacer.set_rate_cap(1.0)
                return "recovered"
            # 兜底：非法迁移显式报错，不静默继续
            raise RuntimeError(f"FSM 非法迁移：{d.state}")

    # -- 跨轮 FSM 状态（写进 last_run.json）--
    def export_fsm(self) -> Dict[str, Any]:
        return {"state": self.fsm.state, "cooldown_s": self.fsm.cooldown_s,
                "fail_rounds": self.fsm.fail_rounds,
                "ramp_index": self.fsm.ramp_index,
                "probe_ok_streak": self.fsm.probe_ok_streak}

    def restore_fsm(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        f = self.fsm
        f.state = data.get("state", "RUNNING")
        f.cooldown_s = float(data.get("cooldown_s", 0))
        f.fail_rounds = int(data.get("fail_rounds", 0))
        f.ramp_index = int(data.get("ramp_index", 0))
        f.probe_ok_streak = int(data.get("probe_ok_streak", 0))
        if f.state == "RAMP":
            self.pacer.set_rate_cap(f.ramp_stages[min(f.ramp_index, len(f.ramp_stages) - 1)])
        elif f.state == "TRIPPED":
            # 熔断不跨轮延续：等下一调度周期重新尝试
            f.reset()
