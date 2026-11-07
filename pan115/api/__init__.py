"""Pan115 —— 面向使用者的统一门面。

把各能力域 API 组合到一个对象上，使用方式：

    from pan115 import Pan115
    pan = Pan115.load()                 # 自动加载 ~/.pan115 配置与凭据
    print(pan.user.info())              # 用户信息
    for f in pan.fs.iter_list("0"):     # 遍历根目录
        print(f["fn"])
    pan.upload.upload_file("a.mkv", "0")   # 上传
    pan.download.download(pc, "./a.mkv")   # 下载
    pan.offline.add_urls(["https://..."], "0")  # 云下载
"""

from __future__ import annotations

import os
from typing import Optional

from ..auth import AuthManager
from ..client import OpenClient
from ..config import Config
from ..tokenstore import TokenStore
from .download import DownloadAPI
from .fs import FsAPI
from .offline import OfflineAPI
from .recycle import RecycleAPI
from .share import ShareAPI
from .upload import UploadAPI
from .user import UserAPI
from .video import VideoAPI


class Pan115:
    """统一门面：client + 各能力域 API + 认证管理。"""

    def __init__(self, client: OpenClient):
        self.client = client
        self.auth = AuthManager(client)
        self.user = UserAPI(client)
        self.fs = FsAPI(client)
        self.upload = UploadAPI(client)
        self.download = DownloadAPI(client)
        self.recycle = RecycleAPI(client)
        self.video = VideoAPI(client)
        self.offline = OfflineAPI(client)
        self.share = ShareAPI(client)  # 官方未开放，预留

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, config: Optional[Config] = None, **client_kwargs) -> "Pan115":
        """从默认配置目录加载配置与凭据构造客户端。"""
        cfg = config or Config.load()
        client = OpenClient(config=cfg, **client_kwargs)
        return cls(client)

    @classmethod
    def from_tokens(cls, access_token: str, refresh_token: str = "") -> "Pan115":
        """直接用已有 token 构造（适合服务端集成，token 由外部管理）。"""
        cfg = Config.load()
        store = TokenStore(cfg.credentials_file)
        client = OpenClient(
            config=cfg, access_token=access_token,
            refresh_token=refresh_token, token_store=store,
        )
        return cls(client)

    # ------------------------------------------------------------------
    # 便捷操作
    # ------------------------------------------------------------------
    def whoami(self) -> dict:
        """当前授权用户信息（同时验证 token 有效性）。"""
        return self.user.info()

    def storage_usage(self) -> dict:
        """容量使用概况: {total, used} 字节数。"""
        info = self.user.info()
        space = info.get("rt_space_info") or info.get("space_info") or {}
        def _size(node):
            if not isinstance(node, dict):
                return 0
            try:
                return int(node.get("size") or 0)
            except (TypeError, ValueError):
                return 0
        return {"total": _size(space.get("all_total")), "used": _size(space.get("all_use"))}

    def resolve_cid(self, path_or_cid: str) -> str:
        """路径或目录ID -> 目录ID。"""
        if path_or_cid in ("0", "") or path_or_cid.isdigit():
            return path_or_cid or "0"
        return self.fs.resolve_path(path_or_cid)

    def find_pick_code(self, name_or_id: str, cid: str = "0") -> str:
        """在指定目录按文件名/ID 查找文件提取码（下载需要）。"""
        for item in self.fs.iter_list(cid):
            fid = str(item.get("fid") or "")
            fn = str(item.get("fn") or "")
            if name_or_id in (fid, fn):
                pc = item.get("pc") or ""
                if pc:
                    return str(pc)
        raise KeyError(f"未找到文件或缺少提取码: {name_or_id}")
