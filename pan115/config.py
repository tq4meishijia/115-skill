"""配置与凭据存储路径。

文件布局（用户主目录下，Windows/macOS/Linux 通用）：
    ~/.pan115/config.json       应用与客户端配置（client_id / client_secret / 限速等）
    ~/.pan115/credentials.json  access_token / refresh_token 持久化（权限 0600）

优先级：命令行参数 > 环境变量 > config.json > 默认值。
环境变量：PAN115_CLIENT_ID、PAN115_CLIENT_SECRET、PAN115_RATE_LIMIT、
         PAN115_CONFIG_DIR（自定义存储目录，便于测试与多账号）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Optional


def default_config_dir() -> Path:
    env = os.environ.get("PAN115_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".pan115"


@dataclass
class Config:
    """全局配置。"""

    client_id: str = ""
    client_secret: str = ""
    #: 客户端限速（请求/秒）。115 开放平台对 API 有限流策略，默认保守 2 rps。
    rate_limit: float = 2.0
    #: 网络层/服务端故障的最大重试次数
    max_retries: int = 3
    #: 单请求超时（秒）
    timeout: float = 30.0
    #: 业务 API 分页默认页大小
    page_size: int = 200
    #: 上传分片大小（字节），默认 20MB
    upload_part_size: int = 20 * 1024 * 1024
    #: 下载/上传使用的 UA。downurl 返回的直链与 UA 绑定，下载时必须带同一 UA。
    user_agent: str = "pan115-manager/1.0"

    config_dir: Path = field(default_factory=default_config_dir)

    # ---------- 路径 ----------
    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def credentials_file(self) -> Path:
        return self.config_dir / "credentials.json"

    # ---------- 加载/保存 ----------
    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        f = cfg.config_file
        if f.exists():
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            for k, v in raw.items():
                if hasattr(cfg, k) and k != "config_dir":
                    setattr(cfg, k, v)
        # 环境变量覆盖
        if os.environ.get("PAN115_CLIENT_ID"):
            cfg.client_id = os.environ["PAN115_CLIENT_ID"]
        if os.environ.get("PAN115_CLIENT_SECRET"):
            cfg.client_secret = os.environ["PAN115_CLIENT_SECRET"]
        if os.environ.get("PAN115_RATE_LIMIT"):
            try:
                cfg.rate_limit = float(os.environ["PAN115_RATE_LIMIT"])
            except ValueError:
                pass
        cfg.config_dir.mkdir(parents=True, exist_ok=True)
        return cfg

    def save(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("config_dir", None)
        data["config_dir"] = str(self.config_dir)
        self.config_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["config_dir"] = str(self.config_dir)
        return d
