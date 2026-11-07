"""文件管理 API。

对应官方文档「文件管理」章节，含目录与文件的增删改查与搜索。
每个方法的 docstring 标注了对应的接口路径、方法与官方文档地址。

文档: https://www.yuque.com/115yun/open/qur839kyx9cgxpxi (章节首页)
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterator, List, Optional
from ..exceptions import ObjectNotFoundError

# 文件列表排序字段
ORDER_FILE_NAME = "file_name"
ORDER_FILE_SIZE = "file_size"
ORDER_USER_UTIME = "user_utime"
ORDER_FILE_TYPE = "file_type"

# 一级筛选大分类
TYPE_DOC, TYPE_IMAGE, TYPE_MUSIC, TYPE_VIDEO, TYPE_ARCHIVE, TYPE_APP = "1", "2", "3", "4", "5", "6"


class FsAPI:
    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list(
        self,
        cid: str = "0",
        *,
        limit: int = None,
        offset: int = 0,
        order: str = ORDER_FILE_NAME,
        asc: bool = True,
        suffix: str = "",
        type_: str = "",
        star: bool = False,
        show_dir: bool = True,
        cur: int = 1,
    ) -> Dict[str, Any]:
        """获取文件列表。

        GET https://proapi.115.com/open/ufile/files
        参数: cid(目录ID,根目录0) type(分类1文档2图片3音乐4视频5压缩包6应用)
             limit offset suffix asc o(排序字段) star(星标) show_dir cur(仅当前目录)
        返回: {"data": [文件对象...], "count": 总数, "path": 父目录树, ...}
        文件对象关键字段: fid(文件ID) fn(文件名) fc(0文件夹/1文件) fs(大小)
                        sha1 pc(提取码) pid(父ID) upt(修改时间) ico(后缀) ism(星标)
        文档: https://www.yuque.com/115yun/open/kz9ft9a7s57ep868
        """
        return self.client.request(
            "GET",
            "/open/ufile/files",
            query={
                "cid": cid,
                "limit": limit or self.client.config.page_size,
                "offset": offset,
                "o": order,
                "asc": 1 if asc else 0,
                "suffix": suffix,
                "type": type_,
                "star": 1 if star else 0,
                "show_dir": 1 if show_dir else 0,
                "cur": cur,
            },
        )

    def iter_list(
        self, cid: str = "0", *, page_size: int = None, **kwargs
    ) -> Iterator[Dict[str, Any]]:
        """分页遍历目录下全部文件（自动翻页）。"""
        page_size = page_size or self.client.config.page_size
        offset = 0
        while True:
            body = self.list(cid, limit=page_size, offset=offset, **kwargs)
            items = body.get("data") or []
            for item in items:
                yield item
            total = int(body.get("count") or 0)
            offset += len(items)
            if not items or offset >= total:
                return

    def folder_info(self, file_id: str) -> Dict[str, Any]:
        """获取文件(夹)详情（按 ID）。

        GET https://proapi.115.com/open/folder/get_info?file_id=xxx
        返回: file_id/file_name/size/size_byte/folder_count/paths(祖先目录树)...
        文档: https://www.yuque.com/115yun/open/rl8zrhe2nag21dfw
        """
        body = self.client.request(
            "GET", "/open/folder/get_info", query={"file_id": file_id}
        )
        return self._normalize_folder_info(body, file_id)

    def folder_info_by_path(self, path: str) -> Dict[str, Any]:
        """按路径获取文件夹详情。

        POST https://proapi.115.com/open/folder/get_info  (form: path=/xxx/yyy)
        路径不存在时 API 返回空数组或 null —— 规范化为 ObjectNotFoundError。
        """
        body = self.client.request("POST", "/open/folder/get_info", form={"path": path})
        return self._normalize_folder_info(body, path)

    @staticmethod
    def _normalize_folder_info(body: Any, key: str) -> Dict[str, Any]:
        # 官方实现存在多种返回形状：{"data": {...}} / 顶层数组 / 顶层对象 / null
        if isinstance(body, list):
            data = body
        elif isinstance(body, dict):
            data = body.get("data", body)
        else:
            data = None
        if isinstance(data, list):
            if not data:
                raise ObjectNotFoundError(f"路径或目录不存在: {key}")
            return data[0]
        if not data or (isinstance(data, dict) and not data.get("file_id")):
            raise ObjectNotFoundError(f"路径或目录不存在: {key}")
        return data

    def search(
        self,
        search_value: str,
        *,
        cid: str = "",
        limit: int = None,
        offset: int = 0,
        file_label: str = "",
        gte_day: str = "",
        lte_day: str = "",
        fc: str = "",
        type_: str = "",
        suffix: str = "",
    ) -> Dict[str, Any]:
        """文件搜索。

        GET https://proapi.115.com/open/ufile/search
        参数: search_value(关键字) cid(限定目录) file_label(标签) gte_day/lte_day(日期范围)
             fc(1只显示文件夹/2只显示文件) type(分类) suffix(后缀) limit/offset
        返回: {"data": [...], "count": 命中总数}
        文档: https://www.yuque.com/115yun/open/ft2yelxzopusus38
        """
        return self.client.request(
            "GET",
            "/open/ufile/search",
            query={
                "search_value": search_value,
                "cid": cid,
                "limit": limit or self.client.config.page_size,
                "offset": offset,
                "file_label": file_label,
                "gte_day": gte_day,
                "lte_day": lte_day,
                "fc": fc,
                "type": type_,
                "suffix": suffix,
            },
        )

    # ------------------------------------------------------------------
    # 增
    # ------------------------------------------------------------------
    def mkdir(self, pid: str, file_name: str) -> Dict[str, Any]:
        """新建文件夹。

        POST https://proapi.115.com/open/folder/add  (form: pid file_name)
        返回: {"file_id": 新目录ID, "file_name": 目录名}
        文档: https://www.yuque.com/115yun/open/qur839kyx9cgxpxi
        """
        body = self.client.request(
            "POST", "/open/folder/add", form={"pid": pid, "file_name": file_name}
        )
        return body.get("data") or body

    # ------------------------------------------------------------------
    # 改
    # ------------------------------------------------------------------
    def rename(self, file_id: str, file_name: str) -> Dict[str, Any]:
        """重命名（文件夹名称限制 255 字节）。

        POST https://proapi.115.com/open/ufile/update  (form: file_id file_name)
        文档: https://www.yuque.com/115yun/open/gyrpw5a0zc4sengm
        """
        body = self.client.request(
            "POST",
            "/open/ufile/update",
            form={"file_id": file_id, "file_name": file_name},
        )
        return body.get("data") or body

    def set_star(self, file_id: str, star: bool) -> Dict[str, Any]:
        """设置/取消星标。

        POST https://proapi.115.com/open/ufile/update  (form: file_id star=1/0)
        文档: https://www.yuque.com/115yun/open/gyrpw5a0zc4sengm
        """
        body = self.client.request(
            "POST",
            "/open/ufile/update",
            form={"file_id": file_id, "star": "1" if star else "0"},
        )
        return body.get("data") or body

    def move(self, file_ids: str, to_cid: str) -> Dict[str, Any]:
        """移动文件(夹)。

        POST https://proapi.115.com/open/ufile/move
        form: file_ids(逗号分隔,可批量) to_cid(目标目录ID,根目录0)
        文档: https://www.yuque.com/115yun/open/vc6fhi2mrkenmav2
        """
        return self.client.request(
            "POST", "/open/ufile/move", form={"file_ids": file_ids, "to_cid": to_cid}
        )

    def copy(self, file_id: str, pid: str, no_dupli: bool = True) -> Dict[str, Any]:
        """复制文件(夹)。

        POST https://proapi.115.com/open/ufile/copy
        form: pid(目标目录) file_id(逗号分隔,可批量) no_dupli(1不允许重名 0允许)
        文档: https://www.yuque.com/115yun/open/lvas49ar94n47bbk
        """
        return self.client.request(
            "POST",
            "/open/ufile/copy",
            form={
                "pid": pid,
                "file_id": file_id,
                "no_dupli": "1" if no_dupli else "0",
            },
        )

    # ------------------------------------------------------------------
    # 删
    # ------------------------------------------------------------------
    def delete(self, file_ids: str, parent_id: str) -> Dict[str, Any]:
        """删除文件(夹)（进入回收站，可用回收站接口还原）。

        POST https://proapi.115.com/open/ufile/delete
        form: file_ids(逗号分隔,可批量) parent_id(所在父目录ID)
        文档: https://www.yuque.com/115yun/open/kt04fu8vcchd2fnb
        """
        return self.client.request(
            "POST",
            "/open/ufile/delete",
            form={"file_ids": file_ids, "parent_id": parent_id},
        )

    # ------------------------------------------------------------------
    # 路径辅助（组合多个接口的高层方法）
    # ------------------------------------------------------------------
    def resolve_path(self, path: str) -> str:
        """把 / 开头的绝对路径解析为目录 ID（根 "/" -> "0"）。

        优先走 folder_info_by_path 单请求；失败时逐级回退目录遍历。
        """
        path = "/" + (path or "").strip("/")
        if path == "/":
            return "0"
        try:
            info = self.folder_info_by_path(path)
            return str(info.get("file_id") or "")
        except ObjectNotFoundError:
            pass
        # 回退：逐级查找
        cid = "0"
        for part in [p for p in path.split("/") if p]:
            cid = self._find_child(cid, part)
        return cid

    def _find_child(self, cid: str, name: str) -> str:
        for item in self.iter_list(cid):
            if str(item.get("fn") or item.get("file_name") or "") == name:
                return str(item.get("fid") or item.get("file_id") or "")
        raise ObjectNotFoundError(f"目录 {name} 不存在(父ID={cid})")

    def stat(self, path_or_id: str) -> Dict[str, Any]:
        """查询文件(夹)信息：接受目录 ID 或绝对路径。"""
        if path_or_id.isdigit() or path_or_id == "0":
            try:
                return self.folder_info(path_or_id)
            except ObjectNotFoundError:
                pass
        return self.folder_info_by_path(path_or_id)
