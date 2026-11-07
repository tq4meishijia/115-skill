"""回收站 API。

- GET  https://proapi.115.com/open/rb/list    回收站列表（分页）
- POST https://proapi.115.com/open/rb/revert  还原（tid 支持逗号分隔多个）
- POST https://proapi.115.com/open/rb/del     删除/清空（tid 支持逗号分隔多个）

rb/list 响应形状特殊：文件对象以 ID 为键平铺在顶层（与 offset/limit/count/rb_pass 并列），
本模块将其规范化为 {"files": [{...}], "count": n} 结构。

文档: https://www.yuque.com/115yun/open/bg7l4328t98fwgex  (列表)
      https://www.yuque.com/115yun/open/gq293z80a3kmxbaq  (还原)
      https://www.yuque.com/115yun/open/gwtof85nmboulrce  (删除/清空)
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

RESERVED_KEYS = {"offset", "limit", "count", "rb_pass", "state", "code", "message", "error", "errno", "data"}


class RecycleAPI:
    def __init__(self, client):
        self.client = client

    def list(self, *, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        """回收站列表（原始规范化）。

        返回: {"files": [{id, file_name, type, file_size, dtime(删除时间),
                          cid(原父目录), parent_name, pick_code, sha1, thumb_url...}],
               "count": 总数, "rb_pass": 是否设置了回收站密码}
        """
        body = self.client.request(
            "GET", "/open/rb/list", query={"limit": limit, "offset": offset}
        )
        files: List[Dict[str, Any]] = []
        for key, value in body.items():
            if key in RESERVED_KEYS or not isinstance(value, dict):
                continue
            item = dict(value)
            item.setdefault("id", key)
            files.append(item)
        count = body.get("count")
        return {
            "files": files,
            "count": int(count) if str(count or "").isdigit() else len(files),
            "rb_pass": body.get("rb_pass"),
        }

    def iter_list(self, *, page_size: int = 100) -> Iterator[Dict[str, Any]]:
        """分页遍历回收站全部条目。"""
        offset = 0
        while True:
            page = self.list(limit=page_size, offset=offset)
            files = page["files"]
            yield from files
            total = page["count"]
            offset += len(files)
            if not files or offset >= total:
                return

    def revert(self, tid: str) -> Dict[str, Any]:
        """还原回收站条目。

        POST https://proapi.115.com/open/rb/revert  (form: tid)
        :param tid: 条目ID，多个以英文逗号分隔
        返回: {tid: {state, error, errno}} —— 每个条目的还原结果
        """
        return self.client.request("POST", "/open/rb/revert", form={"tid": tid})

    def delete(self, tid: str) -> Dict[str, Any]:
        """彻底删除回收站条目（不可恢复！）。

        POST https://proapi.115.com/open/rb/del  (form: tid)
        :param tid: 条目ID，多个以英文逗号分隔；传 "all" 为清空回收站（以官方文档为准）
        """
        return self.client.request("POST", "/open/rb/del", form={"tid": tid})
