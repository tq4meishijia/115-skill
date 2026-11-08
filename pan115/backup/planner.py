"""扫描、忽略规则、全量/增量识别、零碎小文件确定性聚合。

- 与本地状态比对，请求量随「变更量 M」而非「总量 N」增长。
- 快速比对用 (rel_path, size, mtime_ns)，内容权威依据是全文件 SHA1。
- 小文件打成**确定性 tar（不压缩）**：成员按相对路径排序、tar header 归一；
  同一输入两次构建 SHA1 必须相同，否则报错不出包。

``ponytail:`` 不引入压缩（保住「内容不变 -> SHA1 不变 -> 秒传命中」），
不引入按目录分桶。升级路径：单文件变更频繁时按目录局部性分桶。
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..api.upload import hash_file_smart
from .state import StateStore

MiB = 1024 * 1024


@dataclass
class FileEntry:
    abs_path: Path
    rel_path: str            # POSIX 风格，含 label 前缀
    label: str
    size: int
    mtime_ns: int
    sha1: str = ""
    preid: str = ""
    key: str = ""


@dataclass
class Unit:
    """一个上传单元：普通文件或聚合包。"""
    unit_id: str
    kind: str                      # "file" | "pack"
    rel_path: str                  # 状态记录用（包用 pack_id）
    remote_name: str
    remote_dir: str                # 云端相对目录（cloud_root/<label>/<子目录>）
    size: int
    sha1: str
    preid: str
    key: str
    mtime_ns: int = 0
    local_path: Optional[Path] = None    # 真实上传时读取的文件
    members: List[Dict[str, Any]] = field(default_factory=list)
    source_entries: List[FileEntry] = field(default_factory=list)
    pack_id: str = ""


# ----------------------------------------------------------------------
# 忽略规则（纯函数，可离线枚举）
# ----------------------------------------------------------------------
def _norm(p: str) -> str:
    return p.replace("\\", "/").strip("/")


def is_ignored(rel_path: str, settings: Any) -> bool:
    """rel_path 为 POSIX 风格相对路径（不含盘符）。"""
    parts = _norm(rel_path).split("/")
    name = parts[-1]
    for d in parts[:-1]:
        if d in settings.exclude_dir_names:
            return True
    low = _norm(rel_path)
    for prefix in settings.exclude_path_prefixes:
        if low.startswith(_norm(prefix)) or f"/{_norm(prefix)}" in f"/{low}":
            return True
    for pat in settings.exclude_globs:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(low, pat):
            return True
    for pat in settings.include_globs:
        if pat in ("**/*", "*"):
            return False
        if fnmatch.fnmatch(low, pat) or fnmatch.fnmatch(name, pat):
            return False
    return True


def scan_sources(settings: Any, *, stat=os.stat) -> Tuple[List[FileEntry], List[str]]:
    """实时扫描，返回 (条目, scan_error 列表)。不假设存在预置清单。"""
    entries: List[FileEntry] = []
    errors: List[str] = []
    for src in settings.sources:
        if not src.get("enabled", True):
            continue
        root = Path(src["path"])
        label = str(src["label"])
        if not root.is_dir():
            errors.append(f"源目录不可读：{root}")
            continue
        for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
            rel_dir = _norm(os.path.relpath(dirpath, str(root)))
            if rel_dir == ".":
                rel_dir = ""
            # 修剪目录：忽略名单 + 系统链接目录
            kept = []
            for d in dirnames:
                r = f"{label}/{rel_dir + '/' if rel_dir else ''}{d}"
                full = os.path.join(dirpath, d)
                if d in settings.exclude_dir_names or is_ignored(r + "/__probe__", settings):
                    continue
                if os.path.islink(full):
                    continue
                kept.append(d)
            dirnames[:] = kept
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if os.path.islink(full):
                    continue
                rel = f"{label}/{rel_dir + '/' if rel_dir else ''}{fn}"
                if is_ignored(rel, settings):
                    continue
                try:
                    st = stat(full)
                except OSError as e:
                    errors.append(f"stat 失败 {full}: {e}")
                    continue
                if not os.path.isfile(full):
                    continue
                entries.append(FileEntry(abs_path=Path(full), rel_path=rel,
                                         label=label, size=int(st.st_size),
                                         mtime_ns=int(getattr(st, "st_mtime_ns", 0))))
    entries.sort(key=lambda e: e.rel_path)
    return entries, errors


def _win_long_path(p: Path) -> str:
    """Windows 长路径访问前缀（只用于打开，不写进 manifest）。"""
    s = str(p)
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(s)
    return s


def fill_hashes(entries: List[FileEntry], *, workers: int = 1) -> None:
    """复用 api/upload.py::hash_file_smart（整文件 SHA1 + 前 128KB SHA1）。"""
    for e in entries:
        h = hash_file_smart(_win_long_path(e.abs_path))
        e.sha1 = h["fileid"]
        e.preid = h["preid"]
        e.size = h["file_size"]
        e.key = StateStore.key(e.rel_path, e.size, e.mtime_ns, e.sha1)


# ----------------------------------------------------------------------
# 变更识别
# ----------------------------------------------------------------------
def changed_files(entries: List[FileEntry], state: StateStore,
                  *, hash_workers: int = 1) -> Tuple[List[FileEntry], List[FileEntry], int]:
    """返回 (待上传变更文件, 仅需更新元数据的文件, 跳过数)。

    - 本地状态命中相同幂等键 -> skipped（不重算 SHA1）
    - rel_path 已有记录但 (size, mtime_ns) 变化 -> 重算 SHA1
      * SHA1 相同：只更新元数据，不重传
      * SHA1 不同：变更，进入上传计划
    """
    pending: List[FileEntry] = []
    meta_only: List[FileEntry] = []
    skipped = 0
    for e in entries:
        old = state.by_rel_path(e.rel_path)
        if old is not None:
            if (int(old.get("size", -1)) == e.size
                    and int(old.get("mtime_ns", -1)) == e.mtime_ns):
                skipped += 1
                continue
            pending.append(e)
        else:
            pending.append(e)
    if pending:
        fill_hashes(pending, workers=hash_workers)
        still: List[FileEntry] = []
        for e in pending:
            old = state.by_rel_path(e.rel_path)
            if old and old.get("sha1") == e.sha1:
                # 内容未变，只是 mtime/时钟变化：更新元数据，不重传
                e.key = StateStore.key(e.rel_path, e.size, e.mtime_ns, e.sha1)
                meta_only.append(e)
            elif state.uploaded(e.key):
                skipped += 1
            else:
                still.append(e)
        pending = still
    return pending, meta_only, skipped


def _versioned(name: str, sha1: str, fmt: str) -> str:
    stem, suffix = os.path.splitext(name)
    return fmt.format(stem=stem, suffix=suffix, sha1_8=sha1[:8], sha1=sha1)


def remote_name_for(rel_path: str, sha1: str, settings: Any,
                    index: "Any" = None) -> str:
    """同名不同内容 -> 版本化命名（确定性，相同内容派生相同名字）。

    硬边界：绝不删除云端文件。「只留最新版」= 不保存版本链，
    不是物理删除旧文件；恢复按状态文件映射取最新版。
    """
    name = rel_path.split("/")[-1]
    if index is not None and index.name_conflict(name, sha1):
        return _versioned(name, sha1, settings.version_suffix_format)
    return name


# ----------------------------------------------------------------------
# 确定性 tar 聚合
# ----------------------------------------------------------------------
def _tarinfo(rel_path: str, size: int) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(rel_path)
    ti.size = size
    ti.mtime = 0
    ti.uid = 0
    ti.gid = 0
    ti.uname = ""
    ti.gname = ""
    ti.mode = 0o644
    ti.type = tarfile.REGTYPE
    return ti


def build_pack(members: List[FileEntry], settings: Any, pack_id: str,
               *, manifest_name: str = None) -> Tuple[Path, Dict[str, Any]]:
    """产出确定性 tar + 包 SHA1；成员路径越界 / 冲突显式报错。"""
    settings.staging_path.mkdir(parents=True, exist_ok=True)
    mname = manifest_name or settings.pack_manifest_name
    rows = []
    for m in members:
        parts = m.rel_path.split("/")
        if any(p in ("..", ".", "") for p in parts) or m.rel_path.startswith("/"):
            raise ValueError(f"tar 成员路径越界：{m.rel_path}")
        rows.append({"rel_path": m.rel_path, "size": m.size,
                     "mtime_ns": m.mtime_ns, "sha1": m.sha1})
    rows.sort(key=lambda r: r["rel_path"])
    manifest = ("\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")) for r in rows) + "\n").encode("utf-8")

    tar_path = settings.staging_path / f"{pack_id}.tar"
    tmp = tar_path.with_suffix(".tmp")
    ordered = sorted(members, key=lambda m: m.rel_path)
    with open(tmp, "wb") as out:
        with tarfile.open(fileobj=out, mode="w", format=tarfile.GNU_FORMAT) as tf:
            for m in ordered:
                ti = _tarinfo(m.rel_path, m.size)
                with open(_win_long_path(m.abs_path), "rb") as f:
                    tf.addfile(ti, f)
            ti = _tarinfo(mname, len(manifest))
            tf.addfile(ti, io.BytesIO(manifest))
    os.replace(tmp, tar_path)

    # 一次读盘同时算整文件 SHA1 与前 128KB SHA1（与 hash_file_smart 同算法）
    h = hashlib.sha1()
    pre = hashlib.sha1()
    pre_remaining = 128 * 1024
    total = 0
    with open(tar_path, "rb") as f:
        while True:
            chunk = f.read(MiB)
            if not chunk:
                break
            total += len(chunk)
            h.update(chunk)
            if pre_remaining > 0:
                take = chunk[:pre_remaining]
                pre.update(take)
                pre_remaining -= len(take)
    meta = {"sha1": h.hexdigest().upper(), "preid": pre.hexdigest().upper(),
            "size": total, "manifest": rows}
    return tar_path, meta


def make_units(changed: List[FileEntry], settings: Any, index: Any = None,
               *, max_packs: int = None) -> List[Unit]:
    """大文件单独成单元；<4MiB 小文件按 32MiB / 500 个触发聚合。"""
    limit = int(max_packs if max_packs is not None else settings.max_batches_per_run)
    threshold = int(settings.small_file_threshold_bytes)
    trigger_bytes = int(settings.pack_trigger_bytes)
    trigger_count = int(settings.pack_trigger_count)
    max_bytes = int(settings.pack_max_bytes)

    small = [e for e in changed if e.size < threshold]
    large = [e for e in changed if e.size >= threshold]
    units: List[Unit] = []

    for e in large:
        rel_dir = "/".join(e.rel_path.split("/")[:-1])
        units.append(Unit(
            unit_id=f"f:{e.key[:16]}", kind="file", rel_path=e.rel_path,
            remote_name=remote_name_for(e.rel_path, e.sha1, settings, index),
            remote_dir=rel_dir, size=e.size, sha1=e.sha1, preid=e.preid,
            key=e.key, mtime_ns=e.mtime_ns, local_path=e.abs_path,
            source_entries=[e],
        ))

    bucket: List[FileEntry] = []
    acc = 0
    pack_no = 0

    def flush() -> None:
        nonlocal bucket, acc, pack_no
        if not bucket or len(units) >= limit:
            return
        pack_no += 1
        pack_id = hashlib.sha256("|".join(m.key for m in bucket).encode()).hexdigest()[:16]
        tar_path, meta = build_pack(bucket, settings, f"pack_{pack_id}")
        name = f"pack_{pack_id}.tar"
        units.append(Unit(
            unit_id=f"p:{pack_id}", kind="pack", rel_path=f"__packs__/{name}",
            remote_name=remote_name_for(name, meta["sha1"], settings, index),
            remote_dir="__packs__", size=meta["size"], sha1=meta["sha1"],
            preid=meta["preid"], key=StateStore.key(
                f"__packs__/{name}", meta["size"], 0, meta["sha1"]),
            local_path=tar_path, members=meta["manifest"],
            source_entries=list(bucket), pack_id=pack_id,
        ))
        bucket, acc = [], 0

    for e in small:
        if acc + e.size > max_bytes and bucket:
            flush()
        bucket.append(e)
        acc += e.size
        if acc >= trigger_bytes or len(bucket) >= trigger_count:
            flush()
    flush()
    return units
