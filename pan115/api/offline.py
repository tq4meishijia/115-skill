"""云下载（离线下载）API。

- POST https://proapi.115.com/open/offline/add_task_urls   添加链接任务（支持批量，\\n 分隔）
- POST https://proapi.115.com/open/offline/add_task_bt     添加 BT 任务（需先解析种子）
- POST https://proapi.115.com/open/offline/torrent         解析 BT 种子
- GET  https://proapi.115.com/open/offline/get_task_list   任务列表（分页）
- POST https://proapi.115.com/open/offline/del_task        删除任务
- POST https://proapi.115.com/open/offline/clear_task      清空任务
- GET  https://proapi.115.com/open/offline/get_quota_info  配额信息

文档: https://www.yuque.com/115yun/open/zkyfq2499gdn3mty (添加链接任务)
      https://www.yuque.com/115yun/open/svfe4unlhayvluly (添加BT任务)
      https://www.yuque.com/115yun/open/evez3u50cemoict1 (解析种子)
      https://www.yuque.com/115yun/open/av2mluz7uwigz74k (任务列表)
      https://www.yuque.com/115yun/open/pmgwc86lpcy238nw (删除任务)
      https://www.yuque.com/115yun/open/uu5i4urb5ylqwfy4 (清空任务)
      https://www.yuque.com/115yun/open/gif2n3smh54kyg0p (配额)
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# 任务状态
TASK_FAILED, TASK_PENDING, TASK_RUNNING, TASK_DONE = -1, 0, 1, 2


class OfflineAPI:
    def __init__(self, client):
        self.client = client

    def add_urls(self, urls: Iterable[str], wp_path_id: str = "0") -> List[Dict[str, Any]]:
        """添加链接任务（HTTP/magnet/ed2k 等，批量以换行分隔）。

        POST https://proapi.115.com/open/offline/add_task_urls
        form: urls(多个以 \\n 分隔) wp_path_id(保存目录ID)
        返回: [{state, code, message, info_hash, url}] 每条链接的结果
        """
        url_list = [u for u in (s.strip() for s in urls) if u]
        if not url_list:
            raise ValueError("urls 不能为空")
        body = self.client.request(
            "POST",
            "/open/offline/add_task_urls",
            form={"urls": "\n".join(url_list), "wp_path_id": wp_path_id},
        )
        data = body.get("data") or body
        return data if isinstance(data, list) else [data]

    def parse_torrent(self, torrent_sha1: str, pick_code: str) -> Dict[str, Any]:
        """解析已上传到云端的种子文件（先上传种子文件，用其 sha1 与 pick_code）。

        POST https://proapi.115.com/open/offline/torrent
        form: torrent_sha1 pick_code
        返回: info_hash/file_size/torrent_name/file_count/torrent_filelist
        """
        body = self.client.request(
            "POST",
            "/open/offline/torrent",
            form={"torrent_sha1": torrent_sha1, "pick_code": pick_code},
        )
        return body.get("data") or body

    def add_bt(
        self,
        info_hash: str,
        torrent_sha1: str,
        pick_code: str,
        *,
        save_path: str = "0",
        wanted: str = "",
        wp_path_id: str = "0",
    ) -> Dict[str, Any]:
        """添加 BT 任务。

        POST https://proapi.115.com/open/offline/add_task_bt
        form: info_hash wanted(选中的文件序号,逗号分隔,空=全部) save_path
             torrent_sha1 pick_code wp_path_id(保存目录ID)
        """
        return self.client.request(
            "POST",
            "/open/offline/add_task_bt",
            form={
                "info_hash": info_hash,
                "wanted": wanted,
                "save_path": save_path,
                "torrent_sha1": torrent_sha1,
                "pick_code": pick_code,
                "wp_path_id": wp_path_id,
            },
        )

    def list_tasks(self, page: int = 1) -> Dict[str, Any]:
        """获取云下载任务列表。

        GET https://proapi.115.com/open/offline/get_task_list?page=N
        返回: page/page_count/count/tasks([{info_hash, name, percentDone, size,
              status(-1失败 0分配中 1下载中 2成功), file_id, add_time...}])
        """
        body = self.client.request(
            "GET", "/open/offline/get_task_list", query={"page": page}
        )
        return body.get("data") or body

    def delete_task(self, info_hash: str, del_source_file: bool = False) -> Dict[str, Any]:
        """删除云下载任务。

        POST https://proapi.115.com/open/offline/del_task
        form: info_hash del_source_file(1同时删除已下载文件 0保留)
        """
        return self.client.request(
            "POST",
            "/open/offline/del_task",
            form={
                "info_hash": info_hash,
                "del_source_file": "1" if del_source_file else "0",
            },
        )

    def clear_tasks(self, flag: int = 0) -> Dict[str, Any]:
        """清空云下载任务。

        POST https://proapi.115.com/open/offline/clear_task
        form: flag(0全部 1成功 2失败 3进行中 —— 以官方文档为准)
        """
        return self.client.request(
            "POST", "/open/offline/clear_task", form={"flag": str(flag)}
        )

    def quota(self) -> Dict[str, Any]:
        """获取云下载配额信息。

        GET https://proapi.115.com/open/offline/get_quota_info
        返回: count/surplus/used/package([{name, surplus, used, expire_info...}])
        """
        body = self.client.request("GET", "/open/offline/get_quota_info")
        return body.get("data") or body
