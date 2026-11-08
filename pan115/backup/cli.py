"""``backup`` 子命令：run / status / doctor / selftest。

D7 铁律：本模块内部捕获**全部**异常并显式 return 退出码，
绝不让异常冒泡到顶层 ``cli.main()``（那里 ``AuthError -> 3`` 会把授权失效
误报成熔断）。退出码：0 全成功 / 1 部分失败 / 2 授权失效 / 3 熔断 / 4 SKIPPED。
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

from ..api import Pan115
from ..config import Config
from ..exceptions import AuthError, Pan115Error
from .governor import BackupTripped, Governor, ThrottleSignal
from .planner import changed_files, make_units, scan_sources
from .remote_index import RemoteIndex
from .reporter import (EXIT_AUTH, EXIT_OK, EXIT_PARTIAL, EXIT_SKIPPED,
                       EXIT_TRIPPED, Reporter)
from .settings import ConfigError, config_template, load
from .state import LockConflict, RunLock, StateCorruptError, StateStore
from .uploader import run_units


def _eprint(msg: str) -> None:
    print(f"[备份] {msg}", file=sys.stderr)


def _load_settings(args) -> Any:
    path = getattr(args, "config", None) or None
    s, warnings = load(path)
    for w in warnings:
        _eprint(f"配置警告：{w}")
    return s


def _build_client():
    """构造现有门面；强制 PAN115_RATE_LIMIT=1 生效（G1 判据）。"""
    os.environ["PAN115_RATE_LIMIT"] = "1"
    cfg = Config.load()
    cfg.rate_limit = 1.0
    pan = Pan115.load(config=cfg)
    if not pan.client.has_token():
        raise AuthError("尚未授权或凭据已失效：需人工执行 `python -m pan115 auth quicklogin`")
    return pan.client


def cmd_run(args) -> int:
    """一次备份运行。所有异常在此显式映射退出码。"""
    reporter: Optional[Reporter] = None
    lock: Optional[RunLock] = None
    settings = None
    try:
        settings = _load_settings(args)
    except ConfigError as e:
        _eprint(f"配置错误：{e}")
        return EXIT_PARTIAL

    reporter = Reporter(settings, run_id=uuid.uuid4().hex[:12])
    try:
        state = StateStore(settings, logger=lambda ev, **kw: reporter.log(ev, **kw))
        state.load()
        lock = RunLock(settings, logger=lambda ev, **kw: reporter.log(ev, **kw))
        status = lock.acquire(reporter.run_id)
        if status == "skipped":
            reporter.log("lock_conflict", lock_pid=lock.info.get("pid", 0),
                         elapsed_ms=0)
            out = reporter.summary(exit_code=EXIT_SKIPPED, result="skipped")
            print(json.dumps({"result": "skipped", "exit_code": EXIT_SKIPPED,
                              "reason": "已有存活实例"}, ensure_ascii=False))
            return EXIT_SKIPPED
        if status == "takeover":
            _eprint("已接管陈旧锁（详见 lock_conflict 事件）")

        client = _build_client()
        gw = Governor(client, settings, reporter)
        gw.install()
        prior = _read_fsm(settings)
        if prior:
            gw.restore_fsm(prior)

        entries, scan_errors = scan_sources(settings)
        reporter.stats["units_scanned"] = len(entries)
        reporter.stats["scan_errors"] = scan_errors
        changed, meta_only, skipped = changed_files(
            entries, state, hash_workers=int(settings.hash_workers))
        if meta_only:
            for e in meta_only:
                state.append({"key": e.key, "rel_path": e.rel_path, "size": e.size,
                              "mtime_ns": e.mtime_ns, "sha1": e.sha1,
                              "status": "uploaded", "remote_name": e.rel_path.split("/")[-1],
                              "meta_only": True, "run_id": reporter.run_id})

        # 每轮实时建远端索引：只拉本轮涉及变更的目录（请求量随 M 增长）
        index = RemoteIndex(gw, settings, reporter=reporter)
        root = str(settings.cloud_root).strip("/")

        def ensure_dir(rel: str) -> str:
            full = "/".join(p for p in [root, rel.replace("\\", "/")] if p)
            return index.ensure_dir(full)

        # 先产出计划（纯本地，不产生 API），再按计划涉及的目录拉索引
        units = make_units(changed, settings, None)
        rel_dirs = sorted({u.remote_dir for u in units})
        if rel_dirs:
            index.refresh([f"{root}/{d}" if d else root for d in rel_dirs])
        # 索引就绪后重算版本化命名（同名不同内容 -> 确定性派生，不重建 tar）
        from .planner import remote_name_for
        for u in units:
            u.remote_name = remote_name_for(u.rel_path, u.sha1, settings, index)

        deadline = time.monotonic() + float(settings.max_run_duration_s)
        up, skip, fail, resume = run_units(
            units, gateway=gw, settings=settings, reporter=reporter,
            state=state, remote_index=index, ensure_dir=ensure_dir,
            deadline=deadline, clock=time.monotonic)
        reporter.stats["units_skipped"] = skipped + skip
        reporter.stats["units_uploaded"] = up
        reporter.stats["units_failed"] = fail
        reporter.stats["resume_pending"] = resume or bool(scan_errors)
        state.snapshot()

        failed = fail > 0 or bool(scan_errors) or resume
        code = EXIT_PARTIAL if failed else EXIT_OK
        result = "partial" if failed else "ok"
        out = reporter.summary(exit_code=code, result=result, governor=gw, state=state)
        _print_brief(out)
        return code

    except ThrottleSignal as sig:
        # 立即停止本轮后续请求；冷却期内 0 次重试；恢复须单发探测成功
        result = "throttled"
        code = EXIT_PARTIAL
        try:
            active_gw = locals().get("gw")
            if active_gw is not None:
                active_gw.recover(sig)
                result = "throttled_recovered"
            reporter.stats["resume_pending"] = True
            _eprint("频控已恢复（单发探测成功），本轮安全中止，下轮以 RAMP 速率续跑")
        except BackupTripped as e:
            result = "tripped"
            code = EXIT_TRIPPED
            _eprint(f"频控熔断：{e}；已落状态，等下一调度周期再试")
        reporter.summary(exit_code=code, result=result,
                         governor=locals().get("gw"), state=locals().get("state"))
        return code
    except BackupTripped as e:
        _eprint(f"熔断：{e}")
        reporter.summary(exit_code=EXIT_TRIPPED, result="tripped",
                         governor=locals().get("gw"), state=locals().get("state"))
        return EXIT_TRIPPED
    except AuthError as e:
        reporter.log("auth_required", needs_manual_action="reauth", detail=str(e))
        reporter.stats["needs_manual_action"] = "reauth"
        _eprint(f"授权失效，需人工扫码：{e}（禁止自动重授权，不重试）")
        reporter.summary(exit_code=EXIT_AUTH, result="auth_required",
                         governor=locals().get("gw"), state=locals().get("state"))
        return EXIT_AUTH
    except StateCorruptError as e:
        _eprint(f"状态文件损坏（非末尾畸形行，拒绝谎报成功）：{e}")
        reporter.summary(exit_code=EXIT_PARTIAL, result="state_corrupt",
                         governor=locals().get("gw"), state=locals().get("state"))
        return EXIT_PARTIAL
    except (Pan115Error, OSError, ValueError, RuntimeError) as e:
        _eprint(f"运行失败：{type(e).__name__}: {e}")
        reporter.summary(exit_code=EXIT_PARTIAL, result="error",
                         governor=locals().get("gw"), state=locals().get("state"),
                         extra={"error": f"{type(e).__name__}: {e}"})
        return EXIT_PARTIAL
    except Exception as e:  # noqa: BLE001 - D7：兜底，绝不让异常冒泡到 main()
        _eprint(f"未预期失败：{type(e).__name__}: {e}")
        reporter.summary(exit_code=EXIT_PARTIAL, result="error",
                         governor=locals().get("gw"), state=locals().get("state"),
                         extra={"error": f"{type(e).__name__}: {e}"})
        return EXIT_PARTIAL
    finally:
        if lock is not None:
            lock.release()


def _print_brief(out: Dict[str, Any]) -> None:
    print(json.dumps({
        "result": out["result"], "exit_code": out["exit_code"],
        "total_duration_seconds": out["total_duration_seconds"],
        "units_uploaded": out["units_uploaded"], "units_skipped": out["units_skipped"],
        "units_failed": out["units_failed"], "peak_qps": out["peak_qps"],
        "throttle_events": out["throttle_events"],
        "resume_pending": out["resume_pending"],
        "needs_manual_action": out["needs_manual_action"],
    }, ensure_ascii=False))


def _read_fsm(settings) -> Dict[str, Any]:
    try:
        data = json.loads(settings.last_run_path.read_text(encoding="utf-8"))
        return data.get("fsm_state", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cmd_status(args) -> int:
    try:
        s = _load_settings(args)
        data = json.loads(s.last_run_path.read_text(encoding="utf-8"))
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK
    except (ConfigError, OSError, ValueError) as e:
        _eprint(f"读取状态失败：{e}")
        return EXIT_PARTIAL


def cmd_doctor(args) -> int:
    """离线体检：不触网、不写任何文件。"""
    import sys as _sys
    checks: List[Dict[str, Any]] = []
    checks.append({"item": "python", "ok": _sys.version_info >= (3, 9),
                   "detail": _sys.version.split()[0]})
    try:
        import requests  # noqa: F401
        checks.append({"item": "requests", "ok": True, "detail": requests.__version__})
    except ImportError:
        checks.append({"item": "requests", "ok": False,
                       "detail": "缺依赖：python -m pip install requests"})
    template = None
    try:
        s, warns = load(getattr(args, "config", None) or None)
        checks.append({"item": "config", "ok": True, "detail": str(s.path),
                       "warnings": warns})
        checks.append({"item": "sources", "ok": True,
                       "detail": [x["path"] for x in s.sources if x["enabled"]]})
        checks.append({"item": "red_line", "ok": s.api_max_qps <= 1.0 and s.api_concurrency == 1,
                       "detail": f"api_max_qps={s.api_max_qps}, api_concurrency={s.api_concurrency}"})
        from ..tokenstore import TokenStore
        ts = TokenStore(Config.load().credentials_file)
        checks.append({"item": "credentials", "ok": bool(ts.load().get("access_token")),
                       "detail": str(Config.load().credentials_file)})
    except ConfigError as e:
        checks.append({"item": "config", "ok": False, "detail": str(e)})
        template = config_template()
    out = {"checks": checks, "ok": all(c["ok"] for c in checks),
           "os_level_schedule": "disabled"}
    if template is not None:
        out["config_template"] = template
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return EXIT_OK if out["ok"] else EXIT_PARTIAL


def cmd_selftest(args) -> int:
    from .selftest import run_all
    return EXIT_OK if run_all(verbose=not getattr(args, "quiet", False)) else EXIT_PARTIAL


def dispatch(args) -> int:
    """供顶层 cli.py 调用；保证返回 int，不抛异常。"""
    action = getattr(args, "action", "")
    try:
        if action == "run":
            return cmd_run(args)
        if action == "status":
            return cmd_status(args)
        if action == "doctor":
            return cmd_doctor(args)
        if action == "selftest":
            return cmd_selftest(args)
        _eprint(f"未知 backup 子命令：{action}")
        return EXIT_PARTIAL
    except Exception as e:  # noqa: BLE001 - D7 兜底
        _eprint(f"backup 子命令异常：{type(e).__name__}: {e}")
        return EXIT_PARTIAL
