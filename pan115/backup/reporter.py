"""结构化日志、运行摘要与退出码映射。

事件主取值固定为需求说明书 §11.1 的 8 个；更细的信号写 ``event_detail``。
``last_run.json`` 同时输出验收主字段与扩展字段，供定时任务会话只读汇报。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

EVENTS = (
    "api_request", "rate_limited", "retry", "batch_checkpoint",
    "lock_conflict", "auth_required", "file_result", "summary",
)

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_AUTH = 2
EXIT_TRIPPED = 3
EXIT_SKIPPED = 4


class Reporter:
    def __init__(self, settings: Any, *, run_id: str = None, clock=time.time):
        self.s = settings
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._clock = clock
        self._started = clock()
        self.events: List[Dict[str, Any]] = []
        self.stats: Dict[str, Any] = {
            "units_uploaded": 0, "units_skipped": 0, "units_failed": 0,
            "units_scanned": 0, "fast_upload_hits": 0, "init_requests": 0,
            "packs_built": 0, "bytes_uploaded": 0, "failed_terminal": [],
            "resume_pending": False, "needs_manual_action": "", "scan_errors": [],
        }

    # -- 结构化事件 --
    def log(self, event: str, **fields) -> None:
        if event not in EVENTS:
            # 更细粒度信号降级为 event_detail，保证 8 个主取值可检索
            fields = {"event_detail": event, **fields}
            event = "api_request" if "request" in str(fields.get("event_detail", "")).lower() \
                else "summary"
        row = {
            "ts_iso": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "ts_mono": round(time.monotonic(), 6),
            "run_id": self.run_id,
            "level": fields.pop("level", "INFO"),
            "event": event,
            **fields,
        }
        self.events.append(row)
        try:
            p = self.s.log_path
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as e:
            print(f"[备份日志写入失败] {e}", file=__import__("sys").stderr)

    def file_result(self, unit_id: str, status: str, **fields) -> None:
        self.log("file_result", unit_id=unit_id, status=status, **fields)

    def checkpoint(self, batch_id: str, **fields) -> None:
        self.log("batch_checkpoint", batch_id=batch_id, **fields)

    # -- 摘要 --
    def summary(self, *, exit_code: int, result: str, governor: Any = None,
                state: Any = None, extra: Dict[str, Any] = None) -> Dict[str, Any]:
        gw = governor
        duration = max(0.0, self._clock() - self._started)
        n_api = int(getattr(gw, "n_api", 0))
        peak_qps = float(gw.pacer.peak_qps()) if gw is not None else 0.0
        bytes_up = int(self.stats["bytes_uploaded"])
        throttle = dict(getattr(gw, "throttle_events", {})) or {
            "http429": 0, "code_770004": 0, "code_406": 0, "total": 0}
        out: Dict[str, Any] = {
            "run_id": self.run_id,
            "result": result,
            "exit_code": exit_code,
            # 验收主字段（需求说明书 S1⑥）
            "summary.total_duration_seconds": round(duration, 3),
            "summary.bytes_uploaded": bytes_up,
            "summary.api_request_count": n_api,
            "summary.measured_throughput_bps": round(bytes_up / duration, 2) if duration else 0.0,
            "files_total": int(self.stats["units_scanned"]),
            # 扩展字段
            "total_duration_seconds": round(duration, 3),
            "duration_s": round(duration, 3),
            "bytes_uploaded": bytes_up,
            "bytes_transferred": bytes_up,
            "api_request_count": n_api,
            "N_api": n_api,
            "measured_throughput_bps": round(bytes_up / duration, 2) if duration else 0.0,
            "units_scanned": int(self.stats["units_scanned"]),
            "units_uploaded": int(self.stats["units_uploaded"]),
            "units_skipped": int(self.stats["units_skipped"]),
            "units_failed": int(self.stats["units_failed"]),
            "peak_qps": peak_qps,
            "throttle_events": throttle,
            "packs_built": int(self.stats["packs_built"]),
            "init_requests": int(self.stats["init_requests"]),
            "fast_upload_hits": int(self.stats["fast_upload_hits"]),
            "retry_exhausted": int(getattr(gw, "retry_exhausted", 0)),
            "failed_terminal": list(self.stats["failed_terminal"]),
            "resume_pending": bool(self.stats["resume_pending"]),
            "needs_manual_action": self.stats["needs_manual_action"],
            "scan_errors": list(self.stats["scan_errors"]),
            "unknown_warnings": list(getattr(gw, "unknown_warnings", [])),
            "fsm_state": gw.export_fsm() if gw is not None else {},
        }
        if state is not None:
            out["state_convergence_hash"] = state.convergence_hash()
        if extra:
            out.update(extra)
        self.log("summary", **{k: v for k, v in out.items() if k in (
            "result", "exit_code", "total_duration_seconds", "units_uploaded",
            "units_skipped", "units_failed", "peak_qps", "throttle_events",
            "resume_pending", "needs_manual_action")})
        try:
            p = self.s.last_run_path
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2,
                                      sort_keys=True), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            print(f"[摘要写入失败] {e}", file=__import__("sys").stderr)
        return out
