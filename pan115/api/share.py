"""分享管理 API（预留模块）。

重要说明：
    115 开放平台官方文档（https://www.yuque.com/115yun/open，公测版）的能力介绍中
    提到「计划开放分享能力」，但截至本文档当前版本（47 篇 API 文档），
    **尚未发布任何分享相关的开放接口**（无 /open/share/* 端点）。

    因此本模块只做接口预留：方法签名按社区通用分享语义设计（创建/列表/修改/取消），
    全部抛出 NotImplementedError 并附说明。待官方发布分享 API 后，
    在此模块补充实现即可（client.request 已具备全部基础能力）。

    请勿通过非官方（网页端/未公开）接口实现本模块功能：
    开发者协议第 4.3 条明确禁止访问及使用未公开的接口和服务。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_NOT_OPEN = (
    "115 开放平台官方文档当前版本未发布分享 API。"
    "本方法为预留接口，请关注 https://www.yuque.com/115yun/open 的更新。"
)


class ShareAPI:
    """分享管理（官方接口未开放，预留）。"""

    def __init__(self, client):
        self.client = client

    def create(
        self,
        file_ids: str,
        *,
        expire_days: int = 0,
        with_extraction_code: bool = True,
        description: str = "",
    ) -> Dict[str, Any]:
        """创建分享链接（预留）。file_ids 逗号分隔。"""
        raise NotImplementedError(_NOT_OPEN)

    def list(self, *, page: int = 1, limit: int = 20) -> Dict[str, Any]:
        """我的分享列表（预留）。"""
        raise NotImplementedError(_NOT_OPEN)

    def update(self, share_id: str, *, expire_days: Optional[int] = None) -> Dict[str, Any]:
        """修改分享有效期（预留）。"""
        raise NotImplementedError(_NOT_OPEN)

    def cancel(self, share_ids: str) -> Dict[str, Any]:
        """取消分享（预留）。share_ids 逗号分隔。"""
        raise NotImplementedError(_NOT_OPEN)
