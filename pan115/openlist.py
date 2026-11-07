"""OpenList 公益服务（api.oplist.org）令牌获取与刷新。

适用场景：没有自己申请 115 开放平台 AppID/Secret 的用户，借助 OpenList 团队
开源的公共服务（https://github.com/OpenListTeam/OpenList-APIPages）完成
115 OAuth2 授权码换取与令牌刷新。

========================= 认证参数与请求方式说明 =========================

三个端点（均为 GET，参数放 query string；driver 固定为 115cloud）：

1) 获取授权链接
   GET https://api.oplist.org/115cloud/requests
   参数:
     server_use  = true            使用 OpenList 服务端预设的 AppID/Secret（公益模式）
     server_set  = true            同上，前端约定参数
     driver_txt  = 115cloud_go     driver + 类型后缀（115 OAuth2 跳转登录）
     [自备应用时] client_uid=<AppID>&client_key=<AppSecret>，且 server_use 不传或为 false
   返回: {"text": "<登录跳转URL>"}
     text 形如 https://115.com/?ac=open&redirect_uri=<urlencode(115授权URL)>
     内层授权URL: https://passportapi.115.com/open/authorize?client_id=<OpenList的AppID>
                  &state=<随机>&response_type=code&redirect_uri=https://api.oplist.org/115cloud/callback

2) 用户在浏览器完成 115 登录授权（人类必要步骤）
   115 把浏览器 302 到 https://api.oplist.org/115cloud/callback?code=xxx&state=xxx，
   服务端用预设 Secret 换取令牌，再 302 到 https://api.oplist.org/#<base64(JSON)>。

3) 回调结果解析（本模块 parse_callback_input）
   浏览器最终地址 https://api.oplist.org/#<base64> 的 fragment 解码后为 JSON:
     成功: {"access_token": "...", "refresh_token": "...", ...}
     失败: {"message_err": "授权失败，原因: ..."}
   支持粘贴: 完整最终URL / 纯 base64 / 原始 JSON / 直接一对令牌。

4) 刷新令牌（纯程序化，可长期自动化）
   GET https://api.oplist.org/115cloud/renewapi
   参数:
     apps_types  = 115cloud_go
     refresh_ui  = <refresh_token>
     server_use  = true
   返回: 成功 {"access_token": "...", "refresh_token": "..."}
         失败 {"text": "refresh_token 格式错误"}（错误信息同样走 text 字段）

安全提示（引自 OpenList 官方）：
  仅在官方站点(api.oplist.org / api.oplist.org.cn)或你信任的自建站点授权；
  不要分享包含令牌的回调链接；服务端声称不存储任何信息。

已知限制：115 API 开放平台自 2026-08-09 起暂停服务（官方公告），恢复前
requests/callback/renewapi 在 115 侧的交互可能返回失败，本模块会原样透出错误。
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests

from .exceptions import Pan115Error

DEFAULT_BASES = ("https://api.oplist.org", "https://api.oplist.org.cn")
DRIVER = "115cloud"
DRIVER_TXT = "115cloud_go"
REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 2


class OpenListError(Pan115Error):
    """OpenList 公益服务调用失败。"""


@dataclass
class OpenListAuthorizeInfo:
    """/requests 的解析结果。"""

    login_url: str       # 115 登录跳转URL（给用户浏览器打开）
    authorize_url: str   # 内层 115 授权URL（passportapi /open/authorize）
    client_id: str       # 服务端预设的 AppID（自备应用时为传入值）
    state: str           # 授权状态值
    callback_url: str    # 回调地址


def _decode_fragment(fragment: str) -> Dict[str, Any]:
    """解码回调 fragment：base64(JSON)，兼容 urlsafe 变体与缺省 padding。"""
    s = fragment.strip()
    if "=" not in s and len(s) % 4:
        s += "=" * (4 - len(s) % 4)
    try:
        raw = base64.urlsafe_b64decode(s)
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise OpenListError(f"回调数据 base64/JSON 解码失败: {e}") from e
    if not isinstance(data, dict):
        raise OpenListError("回调数据不是 JSON 对象")
    return data


class OpenListAuth:
    """OpenList 公益令牌服务客户端（115cloud）。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASES[0],
        client_uid: str = "",
        client_key: str = "",
        timeout: float = REQUEST_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        session: Optional[requests.Session] = None,
    ):
        self.base = base_url.rstrip("/")
        # 自备应用凭据（可选）；为空即公益模式 server_use=true
        self.client_uid = client_uid
        self.client_key = client_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session or requests.Session()

    # ------------------------------------------------------------------
    # HTTP 基础（带重试与统一错误）
    # ------------------------------------------------------------------
    def _get(self, path: str, params: Dict[str, str]) -> Dict[str, Any]:
        url = f"{self.base}{path}"
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.get(
                    url, params=params, timeout=self.timeout,
                    headers={"User-Agent": "pan115-manager/1.0"},
                )
            except requests.RequestException as e:
                last_err = OpenListError(f"网络请求失败: {e.__class__.__name__}: {e}")
            else:
                if resp.status_code >= 500 and attempt < self.max_retries:
                    last_err = OpenListError(f"服务端错误 HTTP {resp.status_code}")
                    time.sleep(min(2 ** attempt, 4))
                    continue
                if resp.status_code >= 400:
                    raise OpenListError(f"HTTP {resp.status_code}: {(resp.text or '')[:200]}")
                try:
                    return resp.json()
                except ValueError as e:
                    raise OpenListError(f"响应非 JSON: {(resp.text or '')[:200]}") from e
            if attempt < self.max_retries:
                time.sleep(min(2 ** attempt, 4))
        raise last_err or OpenListError("请求失败")

    # ------------------------------------------------------------------
    # 1) 获取授权链接
    # ------------------------------------------------------------------
    def get_authorize_info(self) -> OpenListAuthorizeInfo:
        """调用 /115cloud/requests 获取授权登录链接并解析内层参数。"""
        params: Dict[str, str] = {"driver_txt": DRIVER_TXT}
        if self.client_uid and self.client_key:
            # 自备应用：OpenList 只做回调中转，code 由服务端用你的凭据换取
            params.update({"client_uid": self.client_uid, "client_key": self.client_key})
        else:
            # 公益模式：使用 OpenList 服务端预设的 AppID/Secret
            params.update({"server_use": "true", "server_set": "true"})
        body = self._get(f"/{DRIVER}/requests", params)

        text = body.get("text") or body.get("url") or ""
        if not text:
            raise OpenListError(f"/requests 未返回授权链接: {body}")
        if "message_err" in body:
            raise OpenListError(f"/requests 失败: {body['message_err']}")

        # 解析内层授权URL参数
        login_url = text
        authorize_url = text
        client_id = state = callback = ""
        try:
            qs = parse_qs(urlparse(text).query)
            inner = (qs.get("redirect_uri") or [text])[0]
            authorize_url = unquote(inner)
            qs2 = parse_qs(urlparse(authorize_url).query)
            client_id = (qs2.get("client_id") or [""])[0]
            state = (qs2.get("state") or [""])[0]
            callback = (qs2.get("redirect_uri") or [""])[0]
        except Exception:  # noqa: BLE001  解析失败不阻塞流程，login_url 仍可用
            pass
        return OpenListAuthorizeInfo(login_url, authorize_url, client_id, state, callback)

    # ------------------------------------------------------------------
    # 3) 回调结果解析（支持多种粘贴格式）
    # ------------------------------------------------------------------
    @staticmethod
    def parse_callback_input(text: str) -> Dict[str, Any]:
        """解析用户粘贴的回调结果，返回含 access_token/refresh_token 的 dict。

        接受四种输入（自动识别）:
          1. 浏览器最终完整URL: https://api.oplist.org/#<base64>
          2. 纯 base64 字符串（回调 fragment）
          3. 原始 JSON 文本
          4. 直接一对令牌（两行/空格分隔，或含 access_token= 形式）
        失败统一抛 OpenListError（含服务端 message_err 详情）。
        """
        text = (text or "").strip()
        if not text:
            raise OpenListError("回调输入为空")

        data: Optional[Dict[str, Any]] = None

        # 形式1: URL 带 fragment
        if text.startswith(("http://", "https://")):
            parsed = urlparse(text)
            if parsed.fragment:
                data = _decode_fragment(parsed.fragment)
            else:
                # 直接把 code=... 的回调URL贴进来也可（服务端会拒绝重放，仅用于诊断）
                qs = parse_qs(parsed.query)
                code = (qs.get("code") or [""])[0]
                raise OpenListError(
                    f"该URL不含令牌(fragment)。若粘贴的是带 code= 的中间URL，"
                    f"请在浏览器完成最终跳转后复制地址栏URL（code 值不可复用: {code[:8]}...）"
                )
        # 形式2/3: JSON 或 base64
        elif text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError as e:
                raise OpenListError(f"JSON 解析失败: {e}") from e
        else:
            try:
                data = _decode_fragment(text)
            except OpenListError:
                # 形式4: 直接一对令牌（access 在前，refresh 在后，空白分隔）
                parts = text.split()
                if len(parts) == 2 and all(re.fullmatch(r"[\w.\-]{16,}", p) for p in parts):
                    data = {"access_token": parts[0], "refresh_token": parts[1]}
                else:
                    raise

        if not isinstance(data, dict):
            raise OpenListError("回调数据不是 JSON 对象")

        if data.get("message_err"):
            raise OpenListError(f"服务端返回失败: {data['message_err']}")

        access = data.get("access_token") or ""
        refresh = data.get("refresh_token") or ""
        if not access or not refresh:
            raise OpenListError(f"回调数据缺少令牌字段: {sorted(data.keys())}")
        return data

    # ------------------------------------------------------------------
    # 4) 刷新令牌（纯程序化）
    # ------------------------------------------------------------------
    def renew(self, refresh_token: str) -> Dict[str, Any]:
        """调用 /115cloud/renewapi 刷新令牌，返回 {access_token, refresh_token}。"""
        if not refresh_token:
            raise OpenListError("缺少 refresh_token")
        params: Dict[str, str] = {
            "apps_types": DRIVER_TXT,
            "refresh_ui": refresh_token,
        }
        if self.client_uid and self.client_key:
            params.update({"client_uid": self.client_uid, "client_key": self.client_key})
        else:
            params["server_use"] = "true"
        body = self._get(f"/{DRIVER}/renewapi", params)

        if body.get("access_token") and body.get("refresh_token"):
            return {"access_token": body["access_token"], "refresh_token": body["refresh_token"]}
        # 失败形状: {"text": "错误信息"}
        msg = body.get("text") or body.get("message_err") or json.dumps(body, ensure_ascii=False)[:200]
        raise OpenListError(f"刷新失败: {msg}")

    # ------------------------------------------------------------------
    # 备用：从授权信息直接提取 client_id（可脱离浏览器走设备码PKCE流程）
    # ------------------------------------------------------------------
    @staticmethod
    def extract_client_id(info: OpenListAuthorizeInfo) -> str:
        """提取 OpenList 预设的 115 AppID（如 100197303）。

        提示：拿到 AppID 后也可改走设备码 PKCE 流程（auth.login_by_qrcode），
        免浏览器跳转；是否对该 AppID 开放以 115 服务端为准。
        """
        return info.client_id
