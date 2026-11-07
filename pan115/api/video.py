"""视频播放 API。

- GET  https://proapi.115.com/open/video/play      获取在线播放地址（多清晰度）
- GET  https://proapi.115.com/open/video/history   获取播放进度
- POST https://proapi.115.com/open/video/history   记忆播放进度
- GET  https://proapi.115.com/open/video/subtitle  字幕列表
- POST https://proapi.115.com/open/video/video_push 提交视频转码

文档: https://www.yuque.com/115yun/open/hqglxv3cedi3p9dz (播放地址)
      https://www.yuque.com/115yun/open/gssqdrsq6vfqigag (播放进度-读)
      https://www.yuque.com/115yun/open/bshagbxv1gzqglg4 (播放进度-写)
      https://www.yuque.com/115yun/open/nx076h3glapoyh7u (字幕列表)
      https://www.yuque.com/115yun/open/nxt8r1qcktmg3oan (提交转码)
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class VideoAPI:
    def __init__(self, client):
        self.client = client

    def play_url(self, pick_code: str) -> Dict[str, Any]:
        """获取视频在线播放地址。

        GET https://proapi.115.com/open/video/play?pick_code=xxx
        返回: file_id/file_name/file_size/duration/width/height
             video_url: [{url, definition(1标清~5 4K,100原画), desc}]
        """
        body = self.client.request(
            "GET", "/open/video/play", query={"pick_code": pick_code}
        )
        return body.get("data") or body

    def get_progress(self, pick_code: str) -> Dict[str, Any]:
        """获取视频记忆播放进度。

        GET https://proapi.115.com/open/video/history?pick_code=xxx
        返回: add_time/file_id/file_name/pick_code/time(已播秒数)
        """
        body = self.client.request(
            "GET", "/open/video/history", query={"pick_code": pick_code}
        )
        return body.get("data") or body

    def set_progress(self, pick_code: str, time_sec: int, watch_end: int = 0) -> Dict[str, Any]:
        """记忆视频播放进度。

        POST https://proapi.115.com/open/video/history
        form: pick_code time(进度秒数) watch_end(是否看完 1/0)
        """
        return self.client.request(
            "POST",
            "/open/video/history",
            form={"pick_code": pick_code, "time": str(time_sec), "watch_end": str(watch_end)},
        )

    def subtitles(self, pick_code: str) -> Dict[str, Any]:
        """获取视频字幕列表。

        GET https://proapi.115.com/open/video/subtitle?pick_code=xxx
        返回: autoload(自动加载字幕) list([{sid, language, title, url, type...}])
        """
        body = self.client.request(
            "GET", "/open/video/subtitle", query={"pick_code": pick_code}
        )
        return body.get("data") or body

    def push_transcode(self, pick_code: str, op: str = "vip_push") -> Dict[str, Any]:
        """提交视频转码请求。

        POST https://proapi.115.com/open/video/video_push
        form: pick_code op(操作类型)
        说明: op 取值以官方文档为准；生态内常见取值为 vip_push（加速转码）。
        """
        return self.client.request(
            "POST",
            "/open/video/video_push",
            form={"pick_code": pick_code, "op": op},
        )
