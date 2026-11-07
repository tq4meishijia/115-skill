"""用户信息 API。

- GET https://proapi.115.com/open/user/info  用户信息（含容量、会员状态）

文档: https://www.yuque.com/115yun/open/ot1litggzxa1czww
"""

from __future__ import annotations

from typing import Any, Dict


class UserAPI:
    def __init__(self, client):
        self.client = client

    def info(self) -> Dict[str, Any]:
        """获取当前授权用户信息。

        GET https://proapi.115.com/open/user/info
        返回字段（常用）: user_id / user_name(账号) / user_name_new(昵称) /
        rt_space_info: {all_total: {size, size_format}, all_use: {size, size_format}} 容量
        本接口同时用于验证 access_token 有效性。
        """
        body = self.client.request("GET", "/open/user/info")
        return body.get("data") or body
