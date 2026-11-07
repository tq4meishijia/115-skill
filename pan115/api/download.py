"""文件下载 API。

两步走：
1. POST https://proapi.115.com/open/ufile/downurl  (form: pick_code)
   -> 返回 {file_id: {file_name, file_size, pick_code, sha1, url: {url: 直链}}}
   注意：直链与请求时的 User-Agent 绑定，下载时必须使用同一 UA。
2. 用同一 UA 通过直链流式下载到本地，支持 Range 断点续传。

文档: https://www.yuque.com/115yun/open/um8whr91bxb5997o
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Optional

import requests

from ..exceptions import APIError, NetworkError


class DownloadAPI:
    def __init__(self, client):
        self.client = client

    def get_url(self, pick_code: str, ua: str = None) -> Dict[str, Any]:
        """获取下载直链。

        POST https://proapi.115.com/open/ufile/downurl
        form: pick_code(文件提取码，来自文件列表的 pc 字段)
        返回: {file_id: {file_name, file_size, pick_code, sha1, url: {url}}}
        文档: https://www.yuque.com/115yun/open/um8whr91bxb5997o
        """
        ua = ua or self.client.config.user_agent
        body = self.client.request(
            "POST",
            "/open/ufile/downurl",
            form={"pick_code": pick_code},
            headers={"User-Agent": ua},
        )
        data = body.get("data") or body
        if not isinstance(data, dict) or not data:
            raise APIError("DOWNURL_EMPTY", "未取到下载直链")
        return data

    def download(
        self,
        pick_code: str,
        dest_path: str,
        *,
        ua: str = None,
        resume: bool = True,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> str:
        """下载文件到本地（支持断点续传）。

        :param pick_code: 文件提取码
        :param dest_path: 本地目标路径
        :param ua: 与直链绑定的 UA，默认取客户端配置
        :param resume: 目标文件已存在时从断点继续
        :param progress: progress(loaded_bytes, total_bytes)
        :return: 实际写入的文件路径
        """
        ua = ua or self.client.config.user_agent
        entries = self.get_url(pick_code, ua=ua)
        info = next(iter(entries.values()))
        url = (info.get("url") or {}).get("url")
        if not url:
            raise APIError("DOWNURL_EMPTY", "下载直链为空")
        file_size = int(info.get("file_size") or 0)
        if dest_path in ("", None) or os.path.isdir(dest_path):
            dest_path = os.path.join(dest_path or ".", str(info.get("file_name") or pick_code))

        os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".", exist_ok=True)
        tmp_path = dest_path + ".part"

        done = 0
        if resume and os.path.exists(tmp_path):
            done = os.path.getsize(tmp_path)
            if file_size and done >= file_size:
                os.replace(tmp_path, dest_path)
                return dest_path

        headers = {"User-Agent": ua}
        if done:
            headers["Range"] = f"bytes={done}-"

        attempt = 0
        while True:
            try:
                with requests.get(url, headers=headers, stream=True, timeout=120) as resp:
                    if resp.status_code not in (200, 206):
                        raise APIError(resp.status_code, f"下载失败 HTTP {resp.status_code}")
                    total = int(resp.headers.get("Content-Length") or 0) + (done if resp.status_code == 206 else 0)
                    mode = "ab" if (resp.status_code == 206 and done) else "wb"
                    with open(tmp_path, mode) as f:
                        for chunk in resp.iter_content(chunk_size=1024 * 256):
                            if chunk:
                                f.write(chunk)
                                done += len(chunk)
                                if progress:
                                    progress(done, total or file_size)
                break
            except requests.RequestException as e:
                attempt += 1
                if attempt > 3:
                    raise NetworkError(f"下载中断: {e}") from e
                time.sleep(min(2 ** attempt, 8))
                headers["Range"] = f"bytes={done}-" if done else ""

        os.replace(tmp_path, dest_path)
        return dest_path
