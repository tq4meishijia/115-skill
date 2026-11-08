"""状态持久化与单实例锁（纯 stdlib，无新依赖）。

- ``StateStore``：JSONL 追加日志（权威）+ 周期快照（加速恢复）+ 原子写。
  仅**最后一行**允许按撕裂丢弃并记 warning；非末尾畸形行是硬错误（不谎报成功）。
- ``RunLock``：``O_CREAT|O_EXCL`` 抢锁；存活实例 -> SKIPPED(4)；
  陈旧/畸形锁 -> 接管并记日志。hostname + started_at 双重匹配防 PID 复用误判。

``ponytail:`` 不引入 sqlite / 文件锁服务。升级路径：本地文件数 > 100 万
且恢复耗时 > 60s 时换 SQLite；跨主机共享状态目录时引入 advisory lock。
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_LINE_BYTES = 4 * 1024 * 1024


class StateCorruptError(Exception):
    """非末尾畸形行 / 快照损坏。"""


class LockConflict(Exception):
    """已有存活实例。"""


def _pid_alive(pid: int) -> bool:
    """尽力探测 PID 存活；不支持时返回 True（保守：宁可 SKIPPED 也不并发）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k = ctypes.windll.kernel32
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            k.CloseHandle(h)
            return True
        except Exception:
            return True
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _json_line(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class StateStore:
    """上传单元终态记录。幂等键 = rel_path + size + mtime_ns + sha1。"""

    def __init__(self, settings: Any, *, logger=None):
        self.s = settings
        self._logger = logger
        self.records: Dict[str, Dict[str, Any]] = {}
        self.warnings: List[str] = []
        self.pending: List[Dict[str, Any]] = []
        self._since_snapshot = 0

    # -- 幂等键 --
    @staticmethod
    def key(rel_path: str, size: int, mtime_ns: int, sha1: str) -> str:
        raw = f"{rel_path}|{size}|{mtime_ns}|{sha1}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # -- 加载 --
    def load(self) -> Dict[str, Dict[str, Any]]:
        self.records = {}
        self.warnings = []
        snap = self.s.snapshot_path
        if snap.is_file():
            try:
                data = json.loads(snap.read_text(encoding="utf-8"))
                rows = data.get("records", {}) if isinstance(data, dict) else {}
                if isinstance(rows, dict):
                    self.records = {str(k): dict(v) for k, v in rows.items()
                                    if isinstance(v, dict)}
            except (OSError, ValueError) as e:
                raise StateCorruptError(f"快照损坏：{snap} -> {e}") from e
        j = self.s.journal_path
        if j.is_file():
            raw = j.read_bytes()
            lines = raw.split(b"\n")
            # 末行无换行符时可能是撕裂写；单独判定。
            tail_torn = bool(lines and lines[-1] and not raw.endswith(b"\n"))
            for idx, line in enumerate(lines):
                if not line.strip():
                    continue
                is_last = idx == len(lines) - 1 or all(
                    not x.strip() for x in lines[idx + 1:]
                )
                try:
                    row = json.loads(line.decode("utf-8"))
                    if not isinstance(row, dict) or "key" not in row:
                        raise ValueError("不是合法的单元记录")
                except (ValueError, UnicodeDecodeError) as e:
                    if is_last and (tail_torn or len(line) < MAX_LINE_BYTES):
                        self.warnings.append(f"丢弃撕裂尾行 #{idx + 1}: {e}")
                        continue
                    raise StateCorruptError(
                        f"状态日志第 {idx + 1} 行畸形（非末尾，禁止静默跳过）：{j}"
                    ) from e
                self.records[str(row["key"])] = row
        self.pending = [r for r in self.records.values()
                        if r.get("status") in ("failed", "pending")]
        return self.records

    # -- 查询 --
    def uploaded(self, key: str) -> bool:
        return self.records.get(key, {}).get("status") == "uploaded"

    def by_rel_path(self, rel_path: str) -> Optional[Dict[str, Any]]:
        for r in self.records.values():
            if r.get("rel_path") == rel_path:
                return r
        return None

    # -- 写入 --
    def append(self, record: Dict[str, Any]) -> None:
        self.s.state_path.mkdir(parents=True, exist_ok=True)
        self.records[str(record["key"])] = record
        line = _json_line(record) + "\n"
        with open(self.s.journal_path, "ab") as f:
            f.write(line.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        self._since_snapshot += 1
        if self._since_snapshot >= int(self.s.snapshot_every_units):
            self.snapshot()

    def snapshot(self) -> None:
        self.s.state_path.mkdir(parents=True, exist_ok=True)
        tmp = self.s.snapshot_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"records": self.records}, ensure_ascii=False,
                                  sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.s.snapshot_path)
        self._since_snapshot = 0

    def convergence_hash(self) -> str:
        """除时间戳外的内容指纹，用于 S5「状态文件两次运行后一致」。"""
        rows = []
        for k, r in self.records.items():
            rows.append((k, r.get("status"), r.get("rel_path"), r.get("sha1"),
                         r.get("remote_name"), r.get("size")))
        return hashlib.sha256(_json_line({"rows": sorted(rows)}).encode()).hexdigest()


class RunLock:
    """单实例锁；``acquire()`` 返回 ``acquired`` / ``skipped`` / ``takeover``。"""

    def __init__(self, settings: Any, *, pid_alive=_pid_alive, clock=time.time,
                 logger=None):
        self.s = settings
        self._pid_alive = pid_alive
        self._clock = clock
        self._logger = logger
        self.fd: Optional[int] = None
        self.info: Dict[str, Any] = {}
        self.owned = False       # 只有 acquired/takeover 才可 release

    def acquire(self, run_id: str) -> str:
        self.s.state_path.mkdir(parents=True, exist_ok=True)
        path = self.s.lock_path
        info = {
            "pid": os.getpid(),
            "started_at_wall": self._clock(),
            "hostname": socket.gethostname(),
            "run_id": run_id,
        }
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return self._existing(path, info)
        os.write(fd, json.dumps(info, ensure_ascii=False).encode("utf-8"))
        os.fsync(fd)
        # ponytail: 锁的有效性由「文件存在 + 内容 pid/主机名」判定，
        # 不靠持有 fd。写完即关，避免 Windows 上句柄占用导致 unlink 失败。
        os.close(fd)
        self.fd = None
        self.info = info
        self.owned = True
        return "acquired"

    def _existing(self, path: Path, info: Dict[str, Any]) -> str:
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(old, dict):
                raise ValueError("锁文件不是 JSON 对象")
            age = self._clock() - float(old.get("started_at_wall", 0))
            same_host = old.get("hostname") == info["hostname"]
            alive = bool(self._pid_alive(int(old.get("pid", 0))))
            # 同进程重复触发也必须 SKIPPED（S5 幂等），不能因 pid 相同就判陈旧接管
            if same_host and alive and age < float(self.s.stale_lock_after_s):
                self.info = old
                return "skipped"
            reason = f"stale(age={age:.0f}s,alive={alive},same_host={same_host})"
        except (OSError, ValueError, TypeError) as e:
            reason = f"malformed({type(e).__name__})"
        path.unlink(missing_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, json.dumps(info, ensure_ascii=False).encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        self.fd = None
        self.info = info
        self.owned = True
        if self._logger:
            self._logger("lock_conflict", event_detail="stale_lock_takeover",
                         reason=reason)
        return "takeover"

    def release(self) -> None:
        """只释放自己持有的锁；SKIPPED 实例绝不删别人的锁文件。"""
        if not self.owned:
            return
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        self.s.lock_path.unlink(missing_ok=True)
        self.owned = False
