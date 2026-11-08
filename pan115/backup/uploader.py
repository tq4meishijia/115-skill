"""上传单元编排：复用 api/upload.py 原语 + oss.py，OSS 直传不经 115 网关。

- init（秒传判定）-> 6/7/8 二次校验 -> status=2 秒传（0 字节传输）
  -> 否则 get_token + OSS 直传（小文件 PutObject / 大文件 20MiB 分片）
- 404 同路径重名：版本化改名重排一次（确定性错误，不走退避）
- 大文件中断：/open/upload/resume 配合 OSS 分片续传
- OSS 传输与本地哈希使用有界线程池，**不产生 /open/ 请求**

``ponytail:`` 不重写秒传/签名逻辑，直接复用 UploadAPI 的底层方法与
hash_file_smart；只有编排是新代码。
"""
from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..api.upload import SIGN_CHECK_STATUS, UploadAPI, normalize_callback
from ..exceptions import APIError, ObjectNotFoundError
from ..oss import OssUploader
from .governor import BackupTripped, ThrottleSignal
from .planner import Unit, _versioned, _win_long_path
from .state import StateStore


class UnitFailed(Exception):
    """单元终态失败（已记录，不重试到无限）。"""


def _abort_signals(e: BaseException) -> None:
    """频控 / 熔断必须冒泡到主流程，不能被单元级 except 吞成普通失败。"""
    if isinstance(e, (ThrottleSignal, BackupTripped)):
        raise e


class Uploader:
    def __init__(self, gateway: Any, settings: Any, reporter: Any,
                 *, clock=None):
        self.gw = gateway
        self.s = settings
        self.reporter = reporter
        self.api = UploadAPI(gateway)      # 复用底层原语，governor 作唯一网关
        self.oss = OssUploader(gateway)
        self._clock = clock

    # -- 单个单元的 init 阶段（API 请求，串行经 governor）--
    def _init_once(self, unit: Unit, cid: str, name: str) -> Dict[str, Any]:
        self.reporter.stats["init_requests"] += 1
        return self.api.upload_init(
            file_name=name, file_size=unit.size, target_cid=cid,
            fileid=unit.sha1, preid=unit.preid,
        )

    def prepare(self, unit: Unit, cid: str) -> Dict[str, Any]:
        """返回 {'fast': bool, 'init': ..., 'name': ...}；可能抛 ThrottleSignal。"""
        name = unit.remote_name
        init = self._init_once(unit, cid, name)
        status = int(init.get("status") or 0)
        if status in SIGN_CHECK_STATUS:
            rng = str(init.get("sign_check") or "")
            if "-" in rng:
                start_s, end_s = rng.split("-", 1)
                sign_val = UploadAPI._sha1_range(
                    _win_long_path(unit.local_path), int(start_s), int(end_s))
                self.reporter.stats["init_requests"] += 1
                init = self.api.upload_init(
                    file_name=name, file_size=unit.size, target_cid=cid,
                    fileid=unit.sha1, preid=unit.preid,
                    sign_key=str(init.get("sign_key") or ""), sign_val=sign_val)
                status = int(init.get("status") or 0)
        if status == 2:
            return {"fast": True, "init": init, "name": name}
        return {"fast": False, "init": init, "name": name}

    # -- OSS 阶段（不走 proapi.115.com，不占 1 QPS 预算）--
    def transmit(self, unit: Unit, init: Dict[str, Any]) -> Dict[str, Any]:
        cb = normalize_callback(init)
        bucket, obj = str(init.get("bucket") or ""), str(init.get("object") or "")
        if not bucket or not obj:
            raise UnitFailed(f"init 缺 bucket/object：{unit.unit_id}")
        sts = self.api.get_token()
        part = int(self.s.upload_part_size)
        if unit.size <= part:
            with open(_win_long_path(unit.local_path), "rb") as f:
                data = f.read()
            result = self.oss.put_object(sts, bucket, obj, data, cb)
        else:
            result = self.oss.multipart_upload(
                sts, bucket, obj, _win_long_path(unit.local_path), cb, part)
        if isinstance(result, dict) and result.get("state") in (False, 0, "0"):
            raise UnitFailed(f"上传回调失败：{unit.unit_id} {result}")
        return result if isinstance(result, dict) else {"state": True}

    # -- 对外：处理一个单元 --
    def upload(self, unit: Unit, cid: str, remote_index: Any) -> Dict[str, Any]:
        try:
            prep = self.prepare(unit, cid)
        except (APIError, ObjectNotFoundError) as e:
            code = getattr(e, "code", None)
            if str(code) in ("404", "NOT_FOUND") and unit.remote_name == unit.rel_path.split("/")[-1]:
                # 同路径重名：确定性错误 -> 版本化改名重排一次，不退避
                unit.remote_name = _versioned(unit.remote_name, unit.sha1,
                                              self.s.version_suffix_format)
                prep = self.prepare(unit, cid)
            else:
                raise
        if prep["fast"]:
            self.reporter.stats["fast_upload_hits"] += 1
            return {"status": "uploaded", "fast_upload": True, "bytes": 0,
                    "remote_name": prep["name"], "sha1": unit.sha1}
        result = self.transmit(unit, prep["init"])
        return {"status": "uploaded", "fast_upload": False, "bytes": unit.size,
                "remote_name": prep["name"], "sha1": unit.sha1,
                "fid": str(result.get("file_id") or "")}


def run_units(units: List[Unit], *, gateway: Any, settings: Any, reporter: Any,
              state: StateStore, remote_index: Any, ensure_dir: Callable[[str], str],
              deadline: float, clock, sleeper=None) -> Tuple[int, int, int, bool]:
    """返回 (uploaded, skipped, failed, resume_pending)。

    API init 串行（api_concurrency=1）；OSS 传输用有界池（oss_upload_concurrency）。
    """
    uploader = Uploader(gateway, settings, reporter, clock=clock)
    uploaded = skipped = failed = 0
    resume_pending = False
    batches = 0
    # 按 remote_dir 分组，减少目录解析次数
    groups: Dict[str, List[Unit]] = {}
    for u in units:
        groups.setdefault(u.remote_dir, []).append(u)

    for rel_dir, group in groups.items():
        if clock() >= deadline or batches >= int(settings.max_batches_per_run):
            resume_pending = True
            break
        cid = ensure_dir(rel_dir)
        # L2：远端 SHA1 命中 -> 连 init 都不发
        todo: List[Unit] = []
        for u in group:
            if state.uploaded(u.key) or remote_index.has_sha1(u.sha1):
                state.append({"key": u.key, "rel_path": u.rel_path, "size": u.size,
                              "mtime_ns": u.mtime_ns, "sha1": u.sha1, "status": "uploaded",
                              "remote_name": u.remote_name, "dedup": True,
                              "run_id": reporter.run_id})
                reporter.file_result(u.unit_id, "skipped", reason="dedup")
                skipped += 1
            else:
                todo.append(u)
        # init 串行；OSS 传输在池中并行（不产生 /open/ 请求）
        prepared: List[Tuple[Unit, Dict[str, Any]]] = []
        for u in todo:
            if clock() >= deadline:
                resume_pending = True
                break
            try:
                prep = uploader.prepare(u, cid)
            except Exception as e:  # noqa: BLE001 - 单元级失败须有终态记录
                _abort_signals(e)
                state.append({"key": u.key, "rel_path": u.rel_path, "size": u.size,
                              "mtime_ns": u.mtime_ns, "sha1": u.sha1, "status": "failed",
                              "remote_name": u.remote_name, "error": str(e)[:300],
                              "run_id": reporter.run_id})
                reporter.file_result(u.unit_id, "failed", error=str(e)[:300])
                reporter.stats["failed_terminal"].append(u.rel_path)
                failed += 1
                continue
            if prep["fast"]:
                state.append({"key": u.key, "rel_path": u.rel_path, "size": u.size,
                              "mtime_ns": u.mtime_ns, "sha1": u.sha1, "status": "uploaded",
                              "remote_name": prep["name"], "fast_upload": True,
                              "run_id": reporter.run_id})
                reporter.file_result(u.unit_id, "uploaded", fast_upload=True)
                reporter.stats["fast_upload_hits"] += 1
                uploaded += 1
            else:
                prepared.append((u, prep["init"]))
        if prepared:
            workers = min(int(settings.oss_upload_concurrency), len(prepared))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(uploader.transmit, u, init): u
                           for u, init in prepared}
                for fut in as_completed(futures):
                    u = futures[fut]
                    try:
                        fut.result()
                        state.append({"key": u.key, "rel_path": u.rel_path,
                                      "size": u.size, "mtime_ns": u.mtime_ns, "sha1": u.sha1,
                                      "status": "uploaded",
                                      "remote_name": u.remote_name, "fast_upload": False,
                                      "run_id": reporter.run_id})
                        reporter.file_result(u.unit_id, "uploaded")
                        reporter.stats["bytes_uploaded"] += u.size
                        uploaded += 1
                    except Exception as e:  # noqa: BLE001
                        _abort_signals(e)
                        state.append({"key": u.key, "rel_path": u.rel_path,
                                      "size": u.size, "mtime_ns": u.mtime_ns, "sha1": u.sha1,
                                      "status": "failed", "remote_name": u.remote_name,
                                      "error": str(e)[:300], "run_id": reporter.run_id})
                        reporter.file_result(u.unit_id, "failed", error=str(e)[:300])
                        reporter.stats["failed_terminal"].append(u.rel_path)
                        failed += 1
        batches += 1
        reporter.checkpoint(f"batch_{batches:04d}", remote_dir=rel_dir,
                            units=len(group), uploaded=uploaded, failed=failed)
        for u in group:
            if u.kind == "pack" and u.local_path and u.local_path.exists():
                u.local_path.unlink(missing_ok=True)
        state.snapshot()
    return uploaded, skipped, failed, resume_pending
