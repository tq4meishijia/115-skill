"""远端清单索引：每轮**实时**分页拉取，建 SHA1 / 名称双索引。

- 不做跨轮缓存一致性假设：目录 CID 仅本轮内缓存，文件 SHA1 索引每轮重建。
- 分页完整性显式校验：offset 必须递增到 count，中断即报错（不退化为「空索引」）。
- 索引命中 -> 连 ``upload/init`` 都不发（0 次 API 请求），这是省请求的第一道闸门。

``ponytail:`` 只按需拉取本轮涉及变更的目录，不做全量递归遍历，
把增量轮次的 N_api 压到随 M 增长；升级路径：需要云端对账时加 `verify` 子命令。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from ..api.fs import FsAPI
from ..exceptions import ObjectNotFoundError


class RemoteIndexError(Exception):
    """分页中断 / 目录解析失败。不得退化为「索引为空」导致全量重传。"""


class RemoteIndex:
    def __init__(self, gateway: Any, settings: Any, *, reporter=None):
        self.gw = gateway
        self.s = settings
        self.fs = FsAPI(gateway)
        self.reporter = reporter
        self.by_sha1: Dict[str, List[Dict[str, Any]]] = {}
        self.by_name: Dict[str, str] = {}
        self._cids: Dict[str, str] = {}
        self.pages = 0
        self.warnings: List[str] = []

    # -- 目录 CID（本轮缓存；逐级 mkdir，云端永不删）--
    def ensure_dir(self, rel_path: str) -> str:
        """rel_path 为云端相对路径（cloud_root/<label>/<子目录>...）。"""
        rel = "/".join(p for p in rel_path.replace("\\", "/").split("/") if p)
        if not rel:
            return "0"
        if rel in self._cids:
            return self._cids[rel]
        parts = rel.split("/")
        parent = self.ensure_dir("/".join(parts[:-1])) if len(parts) > 1 else "0"
        full = "/" + rel
        try:
            info = self.fs.folder_info_by_path(full)
            cid = str(info.get("file_id") or "")
            if not cid:
                raise ObjectNotFoundError(f"目录无 file_id: {full}")
        except ObjectNotFoundError:
            out = self.fs.mkdir(parent, parts[-1])
            cid = str(out.get("file_id") or out.get("cid") or "")
            if not cid:
                raise RemoteIndexError(f"mkdir 未返回目录 ID：{full}")
        self._cids[rel] = cid
        return cid

    # -- 实时分页拉取 --
    def refresh(self, rel_dirs: List[str]) -> None:
        seen: Set[str] = set()
        for rel in rel_dirs:
            if rel in seen:
                continue
            seen.add(rel)
            cid = self.ensure_dir(rel)
            self._load_one(cid)

    def _load_one(self, cid: str) -> None:
        limit = int(self.s.page_size)
        offset = 0
        total: Optional[int] = None
        rows = 0
        while True:
            body = self.fs.list(cid, limit=limit, offset=offset)
            items = body.get("data") or []
            if not isinstance(items, list):
                raise RemoteIndexError(f"目录清单形状异常：cid={cid} offset={offset}")
            if total is None:
                try:
                    total = int(body.get("count") or 0)
                except (TypeError, ValueError):
                    raise RemoteIndexError(f"目录清单 count 非法：cid={cid}") from None
            if not items:
                if total > rows:
                    raise RemoteIndexError(
                        f"分页中断（已取 {rows}/{total}，cid={cid} offset={offset}）；"
                        "禁止退化为「索引为空」"
                    )
                break
            for it in items:
                if str(it.get("fc") or "0") == "0":   # 文件夹不进文件索引
                    continue
                sha1 = str(it.get("sha1") or "").upper()
                name = str(it.get("fn") or it.get("file_name") or "")
                if not sha1:
                    self.warnings.append(f"远端条目缺 sha1，按未命中处理：{name}")
                    continue
                self.by_sha1.setdefault(sha1, []).append(it)
                if name:
                    self.by_name[name] = sha1
            offset += len(items)
            rows += len(items)
            self.pages += 1
            if offset >= total:
                break
            if len(items) < limit:
                raise RemoteIndexError(
                    f"分页不足一页但未到 count（{rows}/{total}，cid={cid}）"
                )

    # -- 查询 --
    def has_sha1(self, sha1: str) -> bool:
        return sha1.upper() in self.by_sha1

    def name_conflict(self, name: str, sha1: str) -> bool:
        """同名但内容不同 -> True（触发版本化命名；同名同内容无需处理）。"""
        known = self.by_name.get(name)
        return bool(known and known.upper() != sha1.upper())
