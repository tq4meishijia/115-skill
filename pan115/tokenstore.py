"""access_token / refresh_token 持久化。

凭据保存于 ~/.pan115/credentials.json（POSIX 下权限 0600）。
刷新令牌为轮换式（refresh 会返回新的 refresh_token），因此每次刷新后必须整体落盘。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Config


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    # ---------- 读写 ----------
    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, access_token: str, refresh_token: str, expires_in: int = 0) -> None:
        data = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": expires_in,
            "expires_at": time.time() + expires_in if expires_in else 0,
            "updated_at": time.time(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入，避免中途崩溃产生半截文件
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".cred-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._harden(tmp)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def clear(self) -> None:
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass

    # ---------- 查询 ----------
    def get_access_token(self) -> Optional[str]:
        return self.load().get("access_token")

    def get_refresh_token(self) -> Optional[str]:
        return self.load().get("refresh_token")

    def is_expired(self, margin: int = 60) -> bool:
        data = self.load()
        exp = data.get("expires_at") or 0
        return not data.get("access_token") or (exp and time.time() >= exp - margin)

    # ---------- 内部 ----------
    @staticmethod
    def _harden(path: str) -> None:
        """收紧文件权限（POSIX 0600；Windows 无 POSIX 权限模型，尽力而为）。"""
        if os.name == "posix":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass


def store_from_config(cfg: Config) -> TokenStore:
    return TokenStore(cfg.credentials_file)
