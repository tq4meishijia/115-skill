"""HTTP 客户端核心。

统一负责：
- Bearer 鉴权头注入
- 客户端限速（令牌桶，遵守开放平台限流协议）
- 传输层故障与 5xx/429 的指数退避重试（含 Retry-After 尊重）
- 业务响应规范化解析（state/code/errno/message/error/data）
- 授权失效（401 系错误码）自动刷新 access_token 并重放原请求一次

115 开放平台通用响应形状（各接口略有差异，本客户端做防御式解析）：
    {"state": true|1, "code": 0, "message": "", "data": ..., "error": "", "errno": 0}
授权失效错误码以 "401" 开头（如 40140123-40140126 表示 token 过期/无效）。
"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Any, Callable, Dict, Optional

import requests

from .config import Config
from .exceptions import (
    APIError,
    AuthError,
    NetworkError,
    Pan115Error,
    RateLimitError,
)
from .ratelimit import TokenBucket
from .tokenstore import TokenStore

RETRYABLE_HTTP = (429, 500, 502, 503, 504)


def is_auth_code(code: Any) -> bool:
    """判断是否为授权失效类错误码（401 开头，如 40140123）。"""
    if isinstance(code, int):
        return 40100000 <= code <= 40199999 or 401000 <= code <= 401999
    s = str(code or "")
    return s.startswith("401")


class OpenClient:
    """115 开放平台 HTTP 客户端。"""

    API_BASE = "https://proapi.115.com"
    AUTH_BASE = "https://passportapi.115.com"
    QRCODE_STATUS_URL = "https://qrcodeapi.115.com/get/status/"

    def __init__(
        self,
        config: Optional[Config] = None,
        access_token: str = "",
        refresh_token: str = "",
        token_store: Optional[TokenStore] = None,
        on_tokens_updated: Optional[Callable[[str, str], None]] = None,
        session: Optional[requests.Session] = None,
    ):
        self.config = config or Config.load()
        self.token_store = token_store or TokenStore(self.config.credentials_file)
        self._on_tokens_updated = on_tokens_updated
        self._session = session or requests.Session()

        stored = self.token_store.load()
        self._access_token = access_token or stored.get("access_token", "")
        self._refresh_token = refresh_token or stored.get("refresh_token", "")

        self._limiter = TokenBucket(self.config.rate_limit)
        self._refresh_lock = threading.Lock()

        # 观测钩子：on_request(method, url)、on_retry(err, attempt, delay)、on_refresh()
        self.on_request: Optional[Callable[[str, str], None]] = None
        self.on_retry: Optional[Callable[[Exception, int, float], None]] = None
        self.on_refresh: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------
    # token 管理
    # ------------------------------------------------------------------
    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    def set_tokens(self, access_token: str, refresh_token: str, expires_in: int = 0) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self.token_store.save(access_token, refresh_token, expires_in)
        if self._on_tokens_updated:
            self._on_tokens_updated(access_token, refresh_token)

    def has_token(self) -> bool:
        return bool(self._access_token)

    def refresh_tokens(self) -> None:
        """刷新 access_token（线程安全；轮换式 refresh_token 会一并更新落盘）。"""
        with self._refresh_lock:
            if not self._refresh_token:
                raise AuthError(
                    "没有可用的 refresh_token，请先执行 `pan115 auth login` 完成扫码授权"
                )
            try:
                body = self.request(
                    "POST",
                    "/open/refreshToken",
                    base=self.AUTH_BASE,
                    auth=False,
                    form={"refresh_token": self._refresh_token},
                )
            except AuthError:
                raise
            except Pan115Error as e:
                raise AuthError(f"刷新 access_token 失败: {e}") from e
            data = body.get("data") or {}
            self.set_tokens(
                data.get("access_token", ""),
                data.get("refresh_token", self._refresh_token),
                int(data.get("expires_in", 0)),
            )
            if self.on_refresh:
                self.on_refresh()

    # ------------------------------------------------------------------
    # 核心请求方法
    # ------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        form: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        auth: bool = True,
        base: str = "",
        timeout: Optional[float] = None,
        _auth_retried: bool = False,
    ) -> Dict[str, Any]:
        """发起一次 API 请求并返回解析后的 JSON dict。

        :param path: 完整 URL 或以 / 开头的路径（配合 base 拼接，默认业务域名）
        :param auth: 是否附带 Authorization: Bearer 头
        :raises NetworkError: 重试耗尽后仍失败
        :raises RateLimitError: 限流且重试耗尽
        :raises AuthError: 授权失效且刷新后仍失败
        :raises APIError: 业务失败
        """
        url = path if path.startswith("http") else (base or self.API_BASE) + path
        hdrs: Dict[str, str] = {"User-Agent": self.config.user_agent}
        if auth:
            if not self._access_token:
                raise AuthError("尚未授权：请先执行 `pan115 auth login` 扫码授权")
            hdrs["Authorization"] = f"Bearer {self._access_token}"
        if headers:
            hdrs.update(headers)
        if form is not None:
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")

        attempt = 0
        last_error: Optional[Exception] = None
        while True:
            self._limiter.acquire()
            if self.on_request:
                self.on_request(method, url)
            try:
                resp = self._session.request(
                    method.upper(),
                    url,
                    data=form if form is not None else None,
                    params=query,
                    json=json_body,
                    headers=hdrs,
                    timeout=timeout or self.config.timeout,
                )
            except requests.RequestException as e:
                last_error = NetworkError(f"网络请求失败: {e.__class__.__name__}: {e}")
                if attempt >= self.config.max_retries:
                    raise last_error from e
                self._notify_retry(last_error, attempt)
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            # ---- HTTP 层重试判断 ----
            if resp.status_code in RETRYABLE_HTTP:
                retry_after = self._parse_retry_after(resp)
                last_error = RateLimitError(
                    f"HTTP {resp.status_code}", retry_after=retry_after
                ) if resp.status_code == 429 else APIError(resp.status_code, f"HTTP {resp.status_code}")
                if attempt >= self.config.max_retries:
                    raise last_error
                self._notify_retry(last_error, attempt)
                self._sleep_backoff(attempt, floor=retry_after)
                attempt += 1
                continue

            # ---- 解析响应体 ----
            body = self._parse_json(resp)

            if resp.status_code == 401:
                body = body if isinstance(body, dict) else {}
                if auth and not _auth_retried:
                    self.refresh_tokens()
                    return self.request(
                        method, path, form=form, query=query, json_body=json_body,
                        headers=headers, auth=auth, base=base, timeout=timeout,
                        _auth_retried=True,
                    )
                raise AuthError("访问令牌无效且刷新后仍被拒绝，请重新授权")

            if not isinstance(body, dict):
                # 某些端点成功时直接返回 JSON 数组/原始值
                if resp.status_code < 400:
                    return body if body is not None else {}
                raise APIError(resp.status_code, f"HTTP {resp.status_code}: {str(body)[:200]}")

            # ---- 业务层判断 ----
            state = body.get("state", None)
            ok = state is True or state == 1
            if ok:
                return body

            code = self._first_nonzero(body, ("code", "errno"), fallback=resp.status_code)
            message = (
                body.get("message")
                or body.get("error")
                or f"接口返回失败(HTTP {resp.status_code})"
            )

            if is_auth_code(code):
                if auth and not _auth_retried:
                    self.refresh_tokens()
                    return self.request(
                        method, path, form=form, query=query, json_body=json_body,
                        headers=headers, auth=auth, base=base, timeout=timeout,
                        _auth_retried=True,
                    )
                raise AuthError(f"访问令牌已失效({code}): {message}")

            raise APIError(code, message, data=body)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_json(resp: requests.Response) -> Any:
        text = resp.text or ""
        if not text.strip():
            return None
        try:
            return resp.json()
        except ValueError:
            return {"state": False, "code": -1, "message": f"响应非 JSON: {text[:200]}"}

    @staticmethod
    def _first_nonzero(body: Dict[str, Any], keys: tuple, fallback: Any) -> Any:
        for k in keys:
            v = body.get(k)
            if v not in (None, 0, "", False):
                return v
        return fallback

    @staticmethod
    def _parse_retry_after(resp: requests.Response) -> Optional[float]:
        v = resp.headers.get("Retry-After")
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _sleep_backoff(self, attempt: int, floor: Optional[float] = None) -> None:
        delay = min(2 ** attempt, 8) + random.uniform(0, 0.5)
        if floor is not None and floor > delay:
            delay = min(floor, 30.0)
        time.sleep(delay)

    def _notify_retry(self, err: Exception, attempt: int) -> None:
        if self.on_retry:
            self._notify_quiet(err, attempt)

    def _notify_quiet(self, err: Exception, attempt: int) -> None:
        try:
            self.on_retry(err, attempt, min(2 ** attempt, 8))
        except Exception:
            pass

    def __repr__(self) -> str:
        return f"<OpenClient token={'***' if self._access_token else 'EMPTY'}>"
