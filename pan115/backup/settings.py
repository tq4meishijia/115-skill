"""备份专用配置：加载、schema 校验、红线钳制。

默认路径 ~/.pan115/backup/config.json，可用 PAN115_BACKUP_CONFIG 覆盖。
红线区（api_max_qps / api_concurrency / token_bucket_capacity / 最小间隔）越界
一律报错退出，不静默钳制后继续跑（需求说明书 §10.1）。

设计输入：115 官方未公开任何 QPS 数值；「平台 2 QPS」是用户设定的工程假设，
本功能实际请求速率锁死 <= 1 QPS。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

MiB = 1024 * 1024

DEFAULT_EXCLUDE_DIR_NAMES = [
    "node_modules", "toolchain", ".git", "dist", "build", "__pycache__",
    "venv", "binaries", "logs", "traces", "ErrorReports",
]
DEFAULT_EXCLUDE_PREFIXES = ["plugins/cache", "plugins\\cache", "marketplaces", "security"]
DEFAULT_EXCLUDE_GLOBS = ["*.log", "*.tmp", "*.db-wal", "*.db-shm", "desktop.ini", "thumbs.db"]

#: 全量默认值。所有速率相关取值均为 <=1 QPS 红线下的确定值，无模糊词。
DEFAULTS: Dict[str, Any] = {
    # A. 源 / 目标 / 范围
    "sources": [],                       # [{path, label, enabled}]，必须绝对路径
    "cloud_root": "自动备份",
    "exclude_dir_names": list(DEFAULT_EXCLUDE_DIR_NAMES),
    "exclude_path_prefixes": list(DEFAULT_EXCLUDE_PREFIXES),
    "exclude_globs": list(DEFAULT_EXCLUDE_GLOBS),
    "include_globs": ["**/*"],
    "delete_propagation": False,         # 硬边界：云端永不删
    "keep_policy": "latest_only_no_expiry",
    "version_suffix_format": "{stem}.{sha1_8}{suffix}",
    # C. 限速 / 并发（红线区）
    "api_concurrency": 1,
    "oss_upload_concurrency": 2,
    "hash_workers": 2,
    "api_max_qps": 1.0,
    "min_request_interval_ms": 1100,
    "jitter_ratio": 0.10,
    "jitter_cap_ms": 1200,
    "token_bucket_capacity": 1,
    "page_size": 200,
    "upload_part_size": 20 * MiB,
    "timeout_s": 30,
    # D. 聚合分批
    "small_file_threshold_bytes": 4 * MiB,
    "pack_trigger_bytes": 32 * MiB,
    "pack_trigger_count": 500,
    "pack_min_bytes": 8 * MiB,
    "pack_target_bytes": 96 * MiB,
    "pack_max_bytes": 256 * MiB,
    "pack_format": "tar",
    "pack_manifest_name": "__manifest.jsonl",
    "pack_deterministic": True,
    "batch_checkpoint": True,
    "max_batches_per_run": 200,
    "max_run_duration_s": 14400,
    # E. 重试 / 频控恢复
    "retry_profile": "strict",
    "max_retries": 3,
    "retry_after_cap_s": 30,
    "retry_base_s": 2,
    "retry_factor": 2,
    "retry_cap_s": 60,
    "retry_jitter_ratio": 0.20,
    "retry_max_attempts": 5,
    "throttle_business_codes": [770004, 406],
    "cooldown_initial_s": 300,
    "cooldown_factor": 2,
    "cooldown_cap_s": 3600,
    "probe_qps": 0.5,
    "probe_interval_ms": 2000,
    "probe_per_round": 1,
    "probe_count": 5,
    "probe_path": "/open/user/info",
    "ramp_qps_stages": [0.5, 0.7, 0.9, 1.0],
    "ramp_min_requests": 20,
    "ramp_min_seconds": 60,
    "max_probe_fail_rounds": 3,
    # F. 状态 / 日志 / 反馈
    "state_dir": "",                     # 空 = ~/.pan115/backup
    "state_journal": "state.jsonl",
    "state_snapshot": "state.snapshot.json",
    "snapshot_every_units": 100,
    "lock_file": "run.lock",
    "stale_lock_after_s": 21600,
    "last_run_file": "last_run.json",
    "log_file": "logs/backup.jsonl",
    "log_format": "jsonl",
    "log_level": "INFO",
    "log_max_bytes": 10 * MiB,
    "log_backup_count": 5,
    "staging_dir": "",
}

_RED_LINE_KEYS = (
    "api_max_qps", "api_concurrency", "token_bucket_capacity",
    "min_request_interval_ms", "jitter_ratio", "jitter_cap_ms",
)


class ConfigError(Exception):
    """配置缺失、解析失败或越界。"""


def default_config_path() -> Path:
    env = os.environ.get("PAN115_BACKUP_CONFIG")
    return Path(env).expanduser() if env else Path.home() / ".pan115" / "backup" / "config.json"


def default_state_dir() -> Path:
    return Path.home() / ".pan115" / "backup"


def config_template() -> Dict[str, Any]:
    """供 `backup doctor` 输出的最小可编辑模板（不含用户路径）。"""
    out = dict(DEFAULTS)
    out["sources"] = [{"path": "<本地文件夹绝对路径>", "label": "<云端子目录名>", "enabled": True}]
    out["state_dir"] = str(default_state_dir())
    out["staging_dir"] = str(default_state_dir() / "staging")
    return out


class Settings:
    """只读配置视图：`settings.api_max_qps` 形式访问，未知键抛 AttributeError。"""

    def __init__(self, values: Dict[str, Any], path: Path, warnings: List[str]):
        self._v = values
        self.path = path
        self.warnings = warnings

    def __getattr__(self, key: str) -> Any:
        try:
            return self._v[key]
        except KeyError:
            raise AttributeError(key) from None

    def get(self, key: str, default: Any = None) -> Any:
        return self._v.get(key, default)

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._v)

    # ---- 派生路径（一律绝对路径，不依赖 cwd）----
    @property
    def state_path(self) -> Path:
        return Path(self._v["state_dir"])

    @property
    def staging_path(self) -> Path:
        return Path(self._v["staging_dir"])

    @property
    def journal_path(self) -> Path:
        return self.state_path / self._v["state_journal"]

    @property
    def snapshot_path(self) -> Path:
        return self.state_path / self._v["state_snapshot"]

    @property
    def lock_path(self) -> Path:
        return self.state_path / self._v["lock_file"]

    @property
    def last_run_path(self) -> Path:
        return self.state_path / self._v["last_run_file"]

    @property
    def log_path(self) -> Path:
        return self.state_path / self._v["log_file"]

    @property
    def sources(self) -> List[Dict[str, Any]]:
        return self._v["sources"]


def _load_raw(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise ConfigError(
            f"备份配置不存在：{path}\n"
            "请先用 `python -m pan115 backup doctor` 查看模板并创建配置文件，"
            "或设置环境变量 PAN115_BACKUP_CONFIG 指向配置文件。"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"备份配置解析失败：{path} -> {type(e).__name__}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"备份配置顶层必须是 JSON 对象：{path}")
    return data


def _validate(values: Dict[str, Any]) -> List[str]:
    """返回 warnings；越界直接抛 ConfigError（不静默钳制）。"""
    warnings: List[str] = []

    def _num(key: str, lo: float, hi: float) -> float:
        v = values[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(f"配置 {key} 必须是数值，当前为 {v!r}")
        if not (lo <= float(v) <= hi):
            raise ConfigError(f"配置 {key}={v} 越界，允许范围 [{lo}, {hi}]（<=1 QPS 红线，不自动钳制）")
        return float(v)

    # ---- 红线区 ----
    if float(values["api_max_qps"]) > 1.0:
        raise ConfigError("api_max_qps 不得大于 1.0（用户硬约束：实现不得超过 1 QPS）")
    _num("api_max_qps", 0.1, 1.0)
    if int(values["api_concurrency"]) != 1:
        raise ConfigError("api_concurrency 必须硬固定为 1（全部 /open/ 请求严格串行）")
    if int(values["token_bucket_capacity"]) != 1:
        raise ConfigError("token_bucket_capacity 必须硬固定为 1（禁止突发）")
    _num("min_request_interval_ms", 1000, 1200)
    _num("jitter_ratio", 0.0, 0.20)
    if int(values["jitter_cap_ms"]) > 1200:
        raise ConfigError("jitter_cap_ms 不得大于 1200（平均相邻间隔须 <= 1.2 s）")
    _num("jitter_cap_ms", 1000, 1200)

    # ---- 非红线区：钳制到允许范围（设计 §11 C/D）----
    values["oss_upload_concurrency"] = min(3, max(1, int(values["oss_upload_concurrency"])))
    values["hash_workers"] = min(4, max(1, int(values["hash_workers"])))
    values["page_size"] = min(1000, max(1, int(values["page_size"])))
    values["max_batches_per_run"] = min(1000, max(1, int(values["max_batches_per_run"])))
    values["max_run_duration_s"] = min(28800, max(60, int(values["max_run_duration_s"])))

    # ---- 语义约束 ----
    if values["retry_profile"] not in ("strict", "extended"):
        raise ConfigError("retry_profile 只能是 strict 或 extended")
    if values["delete_propagation"] is not False:
        raise ConfigError("delete_propagation 必须为 false（硬边界：绝不删除云端任何文件）")
    if values["pack_format"] != "tar":
        raise ConfigError("pack_format 必须为 tar（不压缩，保证内容不变 -> SHA1 不变）")
    if values["pack_deterministic"] is not True:
        raise ConfigError("pack_deterministic 必须为 true")
    if not str(values["cloud_root"]).strip() or ".." in str(values["cloud_root"]):
        raise ConfigError("cloud_root 非法")
    if not (values["pack_min_bytes"] <= values["pack_target_bytes"] <= values["pack_max_bytes"]):
        raise ConfigError("单包阈值须满足 pack_min_bytes <= pack_target_bytes <= pack_max_bytes")
    stages = values["ramp_qps_stages"]
    if not isinstance(stages, list) or not stages or max(float(s) for s in stages) > 1.0:
        raise ConfigError("ramp_qps_stages 最大值不得超过 1.0（永不升到 2 QPS）")
    codes = values["throttle_business_codes"]
    if not isinstance(codes, list) or not all(isinstance(c, int) for c in codes):
        raise ConfigError("throttle_business_codes 必须是整数数组")
    values["throttle_business_codes"] = [int(c) for c in codes]
    if int(values["max_probe_fail_rounds"]) < 1:
        raise ConfigError("max_probe_fail_rounds 必须 >= 1")

    # ---- 源目录：禁止假设数据存在 ----
    sources = values["sources"]
    if not isinstance(sources, list) or not sources:
        raise ConfigError("sources 至少需配置一个本地文件夹（禁止假设数据存在）")
    for i, src in enumerate(sources):
        if not isinstance(src, dict) or not src.get("path") or not src.get("label"):
            raise ConfigError(f"sources[{i}] 需含 path 与 label")
        p = Path(str(src["path"])).expanduser()
        if not p.is_absolute():
            raise ConfigError(f"sources[{i}].path 必须是绝对路径：{p}")
        if not p.is_dir():
            raise ConfigError(f"sources[{i}].path 不是可读目录：{p}")
        if ".." in str(src["label"]) or "/" in str(src["label"]) or "\\" in str(src["label"]):
            raise ConfigError(f"sources[{i}].label 不能含路径分隔符或 '..'")
        src["enabled"] = bool(src.get("enabled", True))
        src["path"] = str(p)
    if not any(s["enabled"] for s in sources):
        raise ConfigError("sources 中没有任何启用的目录")

    # ---- 派生目录绝对化 ----
    state_dir = Path(str(values["state_dir"])).expanduser() if values["state_dir"] else default_state_dir()
    staging = Path(str(values["staging_dir"])).expanduser() if values["staging_dir"] else state_dir / "staging"
    if not state_dir.is_absolute() or not staging.is_absolute():
        raise ConfigError("state_dir / staging_dir 必须是绝对路径（禁止用 cwd 推导）")
    values["state_dir"] = str(state_dir)
    values["staging_dir"] = str(staging)
    return warnings


def load(path: Path = None, *, overrides: Dict[str, Any] = None) -> Tuple[Settings, List[str]]:
    """加载并校验；返回 (Settings, warnings)。任何红线越界抛 ConfigError。"""
    cfg_path = Path(path).expanduser() if path else default_config_path()
    raw = _load_raw(cfg_path)
    values: Dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else
                                  list(v) if isinstance(v, list) else v)
                              for k, v in DEFAULTS.items()}
    warnings: List[str] = []
    for key, val in raw.items():
        if key not in values:
            warnings.append(f"未知配置项已忽略：{key}")
            continue
        values[key] = val
    if overrides:
        values.update(overrides)
    warnings = _validate(values) + warnings
    return Settings(values, cfg_path, warnings), warnings


def from_mapping(values: Dict[str, Any]) -> Settings:
    """离线自检用：从内存字典构造，走同一套校验。"""
    merged: Dict[str, Any] = {k: (list(v) if isinstance(v, list) else v)
                              for k, v in DEFAULTS.items()}
    merged.update(values)
    warnings = _validate(merged)
    return Settings(merged, Path("<memory>"), warnings)
