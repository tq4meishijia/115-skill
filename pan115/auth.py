"""认证授权模块。

实现官方文档「接入授权」章节的三种能力：
1. 手机扫码授权（PKCE 模式）——适合桌面/CLI 等无服务端应用
   - POST https://passportapi.115.com/open/authDeviceCode
   - GET  https://qrcodeapi.115.com/get/status/   （长轮询）
   - POST https://passportapi.115.com/open/deviceCodeToToken
2. 授权码模式 —— 适合有服务端参与的 Web 应用
   - GET  https://passportapi.115.com/open/authorize
   - POST https://passportapi.115.com/open/authCodeToToken
3. 刷新 access_token
   - POST https://passportapi.115.com/open/refreshToken

PKCE 说明：code_verifier 为随机串，code_challenge = base64(sha256(code_verifier))
（标准 base64、含 padding）。发起授权时提交 challenge，换取 token 时提交 verifier。

文档: https://www.yuque.com/115yun/open/shtpzfhewv5nag11 (扫码授权)
      https://www.yuque.com/115yun/open/okr2cq0wywelscpe (授权码模式)
      https://www.yuque.com/115yun/open/opnx8yezo4at2be6 (刷新 token)
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional

from .client import OpenClient
from .exceptions import AuthError

#: 二维码轮询状态码（与 115 扫码体系一致）
QR_WAITING = 0     # 未扫描
QR_SCANNED = 1     # 已扫描，待确认
QR_CONFIRMED = 2   # 已确认，授权成功
QR_EXPIRED = -1    # 二维码过期
QR_CANCELED = -2   # 已取消

DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_AUTH_TIMEOUT = 300.0


@dataclass
class DeviceCode:
    """authDeviceCode 的返回数据，即扫码授权会话。"""

    uid: str
    time: int
    qrcode: str   # 二维码内容（用 115 生活 App 扫码）
    sign: str


def generate_pkce_pair() -> tuple:
    """生成 (code_verifier, code_challenge)。challenge=base64(sha256(verifier))。"""
    verifier = secrets.token_urlsafe(64)[:64]
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class AuthManager:
    """认证授权流程封装。所有方法都不要求已有 access_token。"""

    def __init__(self, client: OpenClient):
        self.client = client

    # ------------------------------------------------------------------
    # 1) 设备码 PKCE 扫码授权
    # ------------------------------------------------------------------
    def device_code_begin(self, client_id: str) -> tuple:
        """发起扫码授权：返回 (DeviceCode, code_verifier)。

        code_verifier 必须由调用方保存，授权确认后换取 token 时要用。
        """
        verifier, challenge = generate_pkce_pair()
        body = self.client.request(
            "POST",
            "/open/authDeviceCode",
            base=self.client.AUTH_BASE,
            auth=False,
            form={
                "client_id": client_id,
                "code_challenge": challenge,
                "code_challenge_method": "sha256",
            },
        )
        data = body.get("data") or {}
        device = DeviceCode(
            uid=str(data.get("uid", "")),
            time=int(data.get("time", 0)),
            qrcode=str(data.get("qrcode", "")),
            sign=str(data.get("sign", "")),
        )
        if not device.uid:
            raise AuthError(f"authDeviceCode 返回异常: {body}")
        return device, verifier

    def device_code_status(self, device: DeviceCode) -> Dict[str, Any]:
        """查询扫码状态（长轮询接口，服务端在状态未变化时会挂起请求）。"""
        return self.client.request(
            "GET",
            self.client.QRCODE_STATUS_URL,
            base="",
            auth=False,
            query={"uid": device.uid, "time": device.time, "sign": device.sign},
        )

    @staticmethod
    def parse_qr_status(body: Dict[str, Any]) -> int:
        """从状态接口响应中解析状态码（兼容 data 内嵌与顶层两种返回形状）。"""
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        status = data.get("status", None)
        if status is None:
            # state=0 视为二维码失效
            if body.get("state") in (0, False, "0"):
                return QR_EXPIRED
            raise AuthError(f"无法解析扫码状态响应: {body}")
        return int(status)

    def device_code_wait(
        self,
        device: DeviceCode,
        code_verifier: str,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float = DEFAULT_AUTH_TIMEOUT,
        on_status: Optional[Callable[[int], None]] = None,
    ) -> Dict[str, Any]:
        """阻塞等待用户扫码确认，成功后换取并返回 token dict。

        on_status: 每次状态变化时回调（参数为状态码），用于 CLI 提示。
        """
        deadline = time.monotonic() + timeout
        last: Optional[int] = None
        while time.monotonic() < deadline:
            try:
                body = self.device_code_status(device)
                status = self.parse_qr_status(body)
            except AuthError:
                raise
            except Exception:
                status = QR_WAITING  # 长轮询超时等瞬时问题，继续等待
            if status != last:
                if on_status:
                    on_status(status)
                last = status
            if status == QR_CONFIRMED:
                return self.device_code_to_token(device.uid, code_verifier)
            if status in (QR_EXPIRED, QR_CANCELED):
                raise AuthError(
                    "二维码已过期，请重新发起授权" if status == QR_EXPIRED
                    else "授权已取消"
                )
            time.sleep(poll_interval)
        raise AuthError("等待扫码授权超时，请重试")

    def device_code_to_token(self, uid: str, code_verifier: str) -> Dict[str, Any]:
        """设备码换取 access_token / refresh_token。"""
        body = self.client.request(
            "POST",
            "/open/deviceCodeToToken",
            base=self.client.AUTH_BASE,
            auth=False,
            form={"uid": uid, "code_verifier": code_verifier},
        )
        data = body.get("data") or {}
        if not data.get("access_token"):
            raise AuthError(f"deviceCodeToToken 未返回 access_token: {body}")
        return data

    # ------------------------------------------------------------------
    # 2) 授权码模式
    # ------------------------------------------------------------------
    @staticmethod
    def authorize_url(client_id: str, redirect_uri: str, state: str = None) -> str:
        """构造授权页 URL（引导浏览器打开）。

        redirect_uri 域名需先在开放平台「应用管理-应用域名设置」中登记。
        强烈建议携带随机 state 并在回调时校验，防止 CSRF。
        """
        from urllib.parse import urlencode

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
        }
        if state:
            params["state"] = state
        return f"{OpenClient.AUTH_BASE}/open/authorize?{urlencode(params)}"

    def auth_code_to_token(
        self, client_id: str, client_secret: str, code: str, redirect_uri: str
    ) -> Dict[str, Any]:
        """授权码换取 token（含 client_secret，务必只在服务端调用）。"""
        body = self.client.request(
            "POST",
            "/open/authCodeToToken",
            base=self.client.AUTH_BASE,
            auth=False,
            form={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        data = body.get("data") or {}
        if not data.get("access_token"):
            raise AuthError(f"authCodeToToken 未返回 access_token: {body}")
        return data

    # ------------------------------------------------------------------
    # 3) 刷新 access_token
    # ------------------------------------------------------------------
    def refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        """用 refresh_token 换取新的 token 对（轮换式，旧的立即失效）。"""
        body = self.client.request(
            "POST",
            "/open/refreshToken",
            base=self.client.AUTH_BASE,
            auth=False,
            form={"refresh_token": refresh_token},
        )
        data = body.get("data") or {}
        if not data.get("access_token"):
            raise AuthError(f"refreshToken 失败: {body.get('message') or body}")
        return data

    # ------------------------------------------------------------------
    # 完整扫码登录编排（CLI 友好）
    # ------------------------------------------------------------------
    def login_by_qrcode(
        self,
        client_id: str,
        *,
        save_to=None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float = DEFAULT_AUTH_TIMEOUT,
        on_status: Optional[Callable[[int], None]] = None,
        on_device: Optional[Callable[[DeviceCode], None]] = None,
    ) -> Dict[str, Any]:
        """端到端扫码授权：发起 -> 提示扫码 -> 等待 -> 换 token -> 持久化。"""
        device, verifier = self.device_code_begin(client_id)
        if on_device:
            on_device(device)
        tokens = self.device_code_wait(
            device, verifier, poll_interval=poll_interval, timeout=timeout,
            on_status=on_status,
        )
        if save_to is not None:
            save_to(
                tokens["access_token"],
                tokens.get("refresh_token", ""),
                int(tokens.get("expires_in", 0)),
            )
        return tokens
